"""Real PostGIS integration tests. Use a dedicated disposable database."""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_validation import collection


@pytest.fixture
def client():
    url = os.environ.get("DATABASE_URL", "")
    if not url.rsplit("/", 1)[-1].startswith("spatial_test"):
        pytest.fail("Set DATABASE_URL to a dedicated database named spatial_test*.")
    with TestClient(app) as client:
        with psycopg.connect(url) as conn:
            conn.execute("TRUNCATE features RESTART IDENTITY")
        yield client


def test_roundtrip_filter_pagination_and_not_found(client):
    for x in [0, 10, 20]:
        result = client.post("/features", json=collection({"type": "Point", "coordinates": [x, 0]}))
        assert result.status_code == 201
    first = client.get("/features?limit=2").json()
    assert [f["id"] for f in first["features"]] == [1, 2]
    last = client.get(f"/features?limit=2&after={first['next_after']}").json()
    assert [f["id"] for f in last["features"]] == [3]
    assert last["next_after"] is None
    assert len(client.get("/features?bbox=-1,-1,1,1").json()["features"]) == 1
    assert client.get("/features/1").json()["geometry"]["coordinates"] == [0, 0]
    assert client.get("/features/999").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}


def test_intersection_uses_geometry_not_just_bbox(client):
    # Query falls inside the polygon's hole: bbox overlap is not an intersection.
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]],
        ],
    }
    assert client.post("/features", json=collection(polygon)).status_code == 201
    assert client.get("/features?bbox=1.5,1.5,2,2").json()["features"] == []
    assert len(client.get("/features?bbox=0,0,0.5,0.5").json()["features"]) == 1


def test_batch_atomicity_and_parameterized_properties(client):
    body = collection()
    body["features"].append(collection({"type": "Point", "coordinates": [999, 0]})["features"][0])
    assert client.post("/features", json=body).status_code == 422
    assert client.get("/features").json()["features"] == []
    body = collection()
    body["features"][0]["properties"] = {"name": "'); DROP TABLE features;--"}
    assert client.post("/features", json=body).status_code == 201
    assert client.get("/features/1").json()["properties"] == body["features"][0]["properties"]


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "after=-1", "bbox=1,2,3", "bbox=NaN,0,1,1", "bbox=170,0,-170,1"],
)
def test_invalid_query_has_clear_error(client, query):
    response = client.get(f"/features?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["message"]


def test_invalid_json_content_type_and_size(client):
    assert (
        client.post(
            "/features", content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert client.post("/features", content="{}").status_code == 415
    assert (
        client.post(
            "/features", content=" " * 1_000_001, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
