"""Deliberately strict 2D RFC 7946 subset. Never silently repair input."""

import json
import math

from shapely.geometry import shape
from shapely.validation import explain_validity


class InvalidGeoJSON(ValueError):
    pass


def validate_collection(body: dict) -> list[dict]:
    if body.get("type") != "FeatureCollection":
        raise InvalidGeoJSON("Expected a GeoJSON FeatureCollection.")
    if "crs" in body:
        raise InvalidGeoJSON("CRS members are unsupported. Reproject to WGS84 longitude/latitude.")
    features = body.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= 100:
        raise InvalidGeoJSON("Provide between 1 and 100 features per atomic batch.")
    for i, feature in enumerate(features):
        try:
            validate_feature(feature)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise InvalidGeoJSON(f"Feature {i}: {error}") from error
    return features


def validate_feature(feature: dict) -> None:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise InvalidGeoJSON("Expected a Feature object.")
    if "crs" in feature:
        raise InvalidGeoJSON("CRS members are unsupported; use WGS84 longitude/latitude.")
    props = feature.get("properties")
    if props is not None and not isinstance(props, dict):
        raise InvalidGeoJSON("Properties must be an object or null.")
    # Reject NaN/Infinity anywhere, including properties (JSONB cannot store them).
    json.dumps(feature, allow_nan=False)
    geom = feature.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") not in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    }:
        raise InvalidGeoJSON("Provide a non-null point, line or polygon geometry.")
    if "crs" in geom:
        raise InvalidGeoJSON("CRS members are unsupported; use WGS84 longitude/latitude.")
    positions = []

    depth = {
        "Point": 0,
        "MultiPoint": 1,
        "LineString": 1,
        "MultiLineString": 2,
        "Polygon": 2,
        "MultiPolygon": 3,
    }[geom["type"]]

    def check_depth(coords, remaining):
        if not isinstance(coords, list) or not coords:
            raise InvalidGeoJSON("Coordinates must be nonempty arrays.")
        if remaining == 0:
            if any(isinstance(value, list) for value in coords):
                raise InvalidGeoJSON("Malformed geometry coordinate nesting.")
        else:
            for child in coords:
                check_depth(child, remaining - 1)

    check_depth(geom.get("coordinates"), depth)

    def walk(coords):
        if not isinstance(coords, list) or not coords:
            raise InvalidGeoJSON("Coordinates must be nonempty arrays.")
        if isinstance(coords[0], list):
            for child in coords:
                walk(child)
        else:
            if len(coords) != 2 or any(
                isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                for n in coords
            ):
                raise InvalidGeoJSON(
                    "Positions must contain two finite numbers: longitude, latitude."
                )
            lon, lat = coords
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise InvalidGeoJSON("Coordinates outside WGS84 bounds; check CRS and axis order.")
            positions.append(coords)
            if len(positions) > 10_000:
                raise InvalidGeoJSON("Maximum 10,000 positions per feature.")

    walk(geom.get("coordinates"))
    rings = []
    if geom["type"] == "Polygon":
        rings = geom["coordinates"]
    elif geom["type"] == "MultiPolygon":
        rings = [ring for polygon in geom["coordinates"] for ring in polygon]
    for ring in rings:
        if len(ring) < 4 or ring[0] != ring[-1]:
            raise InvalidGeoJSON(
                "Polygon rings must be explicitly closed with at least 4 positions."
            )
    # Avoid misleading planar queries on dateline-crossing geometries.
    if max(p[0] for p in positions) - min(p[0] for p in positions) > 180:
        raise InvalidGeoJSON("Split geometries spanning more than 180 degrees before ingestion.")
    try:
        geometry = shape(geom)
    except Exception as error:
        raise InvalidGeoJSON("Malformed geometry coordinate nesting.") from error
    if geometry.is_empty or not geometry.is_valid:
        raise InvalidGeoJSON(f"Invalid geometry: {explain_validity(geometry)}")
