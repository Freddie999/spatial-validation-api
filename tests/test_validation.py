import copy

import pytest

from app.validation import InvalidGeoJSON, validate_collection


def collection(geometry=None):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": geometry or {"type": "Point", "coordinates": [0, 0]},
            }
        ],
    }


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [181, 0]},
        {"type": "Point", "coordinates": [0, 91]},
        {"type": "Point", "coordinates": [True, 0]},
        {"type": "Point", "coordinates": [0, 0, 10]},
        {"type": "LineString", "coordinates": [[179, 0], [-179, 0]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
        {"type": "Point", "coordinates": []},
        {"type": "Point", "coordinates": [float("nan"), 0]},
        {"type": "Point", "coordinates": [[0, 0]]},
        {"type": "GeometryCollection", "geometries": []},
    ],
)
def test_reject_bad_geometry(geometry):
    with pytest.raises(InvalidGeoJSON):
        validate_collection(collection(geometry))


@pytest.mark.parametrize("level", ["collection", "feature", "geometry"])
def test_reject_crs_declarations(level):
    body = collection()
    target = {
        "collection": body,
        "feature": body["features"][0],
        "geometry": body["features"][0]["geometry"],
    }[level]
    target["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    with pytest.raises(InvalidGeoJSON, match="CRS"):
        validate_collection(body)


def test_valid_polygon_with_hole_and_no_mutation():
    body = collection(
        {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
            ],
        }
    )
    original = copy.deepcopy(body)
    assert validate_collection(body) == original["features"]
    assert body == original


def test_batch_limits():
    for features in [[], collection()["features"] * 101]:
        with pytest.raises(InvalidGeoJSON):
            validate_collection({"type": "FeatureCollection", "features": features})
