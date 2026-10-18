# Spatial-data validation API

A compact Python/FastAPI service for checking GeoJSON before it enters a spatial database. It rejects ambiguous input, inserts valid batches atomically into PostGIS, and exposes indexed spatial intersection queries with cursor pagination.

**Status:** runnable engineering demonstration, not a deployed production service. Built for Frederick Abalo's portfolio. No employer code or operational data is included. Sample survey features are synthetic.

![Local API demonstration](docs/demo.jpg)

## Try it in five minutes

Requires Docker Desktop/Engine with Compose. The database image uses amd64 emulation on Apple Silicon.

```sh
git clone https://github.com/Freddie999/spatial-validation-api.git
cd spatial-validation-api
docker compose up --build -d
curl http://localhost:8000/health
curl -X POST http://localhost:8000/features \
  -H 'Content-Type: application/geo+json' --data-binary @samples/features.geojson
curl 'http://localhost:8000/features?bbox=-105.31,39.99,-105.27,40.03&limit=1'
# Follow next_after in the response, preserving bbox and limit:
curl 'http://localhost:8000/features?bbox=-105.31,39.99,-105.27,40.03&limit=1&after=1'
```

Open **http://localhost:8000** for the small ingestion demo, or **http://localhost:8000/docs** for the interactive endpoint reference. Data persists in a named Docker volume. `docker compose down` stops services without deleting it. Repeated submissions create new records.

## API contract

| Endpoint | Behavior |
|---|---|
| `POST /features` | Atomic FeatureCollection ingestion; returns inserted count and generated IDs |
| `GET /features?bbox=w,s,e,n&limit=20&after=0` | Exact 2D intersection, ordered IDs, exclusive cursor; `next_after: null` ends the result |
| `GET /features/{id}` | One feature or structured 404 |
| `GET /health` | Database readiness |

The supported RFC 7946 subset is 2D Point/MultiPoint, LineString/MultiLineString, Polygon/MultiPolygon, in WGS84 longitude/latitude order. Limits: 1 MB request, 100 features per batch, 10,000 positions per feature, 100 results per page. Polygon rings must close explicitly; invalid topology, empty/null geometry, nonfinite values, legacy CRS members, and longitude spans over 180° are rejected. Reproject or split input before submission. **Bounds checks cannot prove CRS or detect swapped axes when both values are plausible.**

Example error (HTTP 422):

```json
{"error":{"code":"422","message":"Feature 0: Coordinates outside WGS84 bounds; check CRS and axis order."}}
```

## Architecture and contribution

```mermaid
flowchart LR
    Client[GeoJSON client] --> Limit[Bounded request reader]
    Limit --> Validate[Structural + Shapely validation]
    Validate --> Transaction[Parameterized atomic insert]
    Transaction --> DB[(PostGIS / GiST index)]
    Query[Bounding box + cursor] --> Intersect[ST_Intersects + ordered limit]
    Intersect --> DB
```

This independent example implements the validation boundary, transaction/query layer, demo UI, container setup, and unit/integration test suite. It illustrates engineering concerns from spatial-data systems without reproducing proprietary implementations.

Design choices:

- **Reject, do not repair:** automatic geometry repair can change meaning. Clients receive a feature index and reason.
- **PostGIS is the query engine:** geometry remains typed with SRID 4326; GiST supports intersection filtering. SQL values are bound parameters. Database checks backstop application validation.
- **Keyset pagination:** stable ID order avoids offset scans. It is not a snapshot; later inserts can appear on later pages. Submitted feature IDs are ignored in favor of database-generated IDs.
- **One transaction per batch:** no partially accepted batch. Validation finishes before the write starts.
- **Small connection pool:** blocking writes run in a worker thread; sync reads use FastAPI's thread pool.
- **Simple demo bootstrap:** schema creation occurs at startup. A production service would use versioned migrations and separate DB roles.

## Tests and development

Python is managed by uv; exact resolved dependencies are in `uv.lock`.

```sh
uv sync --frozen
docker compose up -d db
docker compose exec db createdb -U demo spatial_test
DATABASE_URL=postgresql://demo:local-demo-only@localhost:55432/spatial_test uv run pytest -q
uv run ruff check .
```

Integration tests require a **dedicated database named `spatial_test*`**, truncate its features table, and fail explicitly if it is missing. They exercise real PostGIS, including polygon-hole intersection, rollback on invalid batches, round trips, pagination, query validation, and parameterized properties. Unit tests cover geometry structure, topology, CRS declarations, coordinate bounds, and batch limits. CI runs both suites and builds the Docker image.

## Limits and next steps

No authentication, tenant isolation, rate limits, idempotency, reprojection, nearest-neighbor queries, updates, or deletions. Binds to localhost by default; demo credentials are deliberately local-only. Do not expose the unauthenticated service publicly. Geographic intersection is planar in lon/lat, not an ellipsoidal distance operation. GeometryCollections, Z coordinates, and antimeridian-spanning features are outside this contract. Large jobs belong in an asynchronous ingestion pipeline.

## References and attribution

Synthetic sample data was written for this project. Implementation references: [RFC 7946](https://www.rfc-editor.org/rfc/rfc7946), [PostGIS](https://postgis.net/docs/), [FastAPI](https://fastapi.tiangolo.com/), and [Shapely](https://shapely.readthedocs.io/). See [LICENSE](LICENSE) for the independently written code.
