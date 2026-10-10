import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.validation import InvalidGeoJSON, validate_collection

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS features (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 properties jsonb NOT NULL,
 geom geometry(Geometry,4326) NOT NULL,
 CHECK (ST_IsValid(geom)), CHECK (NOT ST_IsEmpty(geom)), CHECK (ST_NDims(geom)=2)
);
CREATE INDEX IF NOT EXISTS features_geom_gix ON features USING gist(geom);
"""


@asynccontextmanager
async def lifespan(app):
    pool = ConnectionPool(
        os.environ["DATABASE_URL"],
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    pool.open()
    pool.wait(timeout=20)
    with pool.connection() as conn:
        conn.execute(SCHEMA)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Spatial validation API",
    version="0.1.0",
    lifespan=lifespan,
    description="Engineering demonstration: bounded, atomic WGS84 GeoJSON ingestion.",
)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return JSONResponse(
        {"error": {"code": str(exc.status_code), "message": exc.detail}},
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "invalid_request",
                "message": "Invalid query or path.",
                "fields": [str(e["loc"]) for e in exc.errors()],
            }
        },
        status_code=422,
    )


@app.exception_handler(psycopg.OperationalError)
async def unavailable(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "database_unavailable",
                "message": "Database unavailable; retry later.",
            }
        },
        status_code=503,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo():
    return Path(__file__).with_name("demo.html").read_text()


@app.get("/health")
def health():
    with app.state.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post(
    "/features",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/geo+json": {
                    "schema": {
                        "type": "object",
                        "required": ["type", "features"],
                        "properties": {
                            "type": {"type": "string", "enum": ["FeatureCollection"]},
                            "features": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {"type": "object"},
                            },
                        },
                    },
                    "example": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"name": "Synthetic point"},
                                "geometry": {"type": "Point", "coordinates": [-105.29, 40.01]},
                            }
                        ],
                    },
                }
            },
        }
    },
)
async def ingest(request: Request):
    if request.headers.get("content-type", "").split(";")[0] not in {
        "application/json",
        "application/geo+json",
    }:
        raise HTTPException(415, "Use application/json or application/geo+json.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1_000_000:
            raise HTTPException(413, "Maximum request size is 1 MB.")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
        if not isinstance(body, dict):
            raise InvalidGeoJSON("Expected a FeatureCollection object.")
        features = validate_collection(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise HTTPException(422, str(error)) from error
    return await run_in_threadpool(insert_features, features)


def insert_features(features):
    # All features are validated before one transaction inserts the complete batch.
    ids = []
    with app.state.pool.connection() as conn:
        for feature in features:
            row = conn.execute(
                "INSERT INTO features(properties,geom) VALUES (%s,ST_SetSRID("
                "ST_GeomFromGeoJSON(%s),4326)) RETURNING id",
                (Jsonb(feature.get("properties") or {}), json.dumps(feature["geometry"])),
            ).fetchone()
            ids.append(row["id"])
    return {"inserted": len(ids), "ids": ids}


def as_feature(row):
    return {
        "type": "Feature",
        "id": row["id"],
        "properties": row["properties"],
        "geometry": row["geometry"],
    }


@app.get("/features")
def list_features(
    bbox: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: Annotated[int, Query(ge=0)] = 0,
):
    conditions, params = ["id > %s"], [after]
    if bbox is not None:
        try:
            west, south, east, north = [float(v) for v in bbox.split(",")]
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError
        except ValueError:
            raise HTTPException(
                422, "bbox must be west,south,east,north in WGS84, without wrapping."
            )
        conditions.append("ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))")
        params.extend([west, south, east, north])
    params.append(limit + 1)
    with app.state.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id,properties,ST_AsGeoJSON(geom)::json AS geometry FROM features WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id LIMIT %s",
            params,
        ).fetchall()
    page = rows[:limit]
    return {
        "type": "FeatureCollection",
        "features": [as_feature(row) for row in page],
        "next_after": page[-1]["id"] if len(rows) > limit else None,
    }


@app.get("/features/{feature_id}")
def get_feature(feature_id: int):
    with app.state.pool.connection() as conn:
        row = conn.execute(
            "SELECT id,properties,ST_AsGeoJSON(geom)::json AS geometry FROM features WHERE id=%s",
            (feature_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Feature not found.")
    return as_feature(row)
