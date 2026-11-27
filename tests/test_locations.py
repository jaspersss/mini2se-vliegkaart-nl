#!/usr/bin/env python3
"""
Regressietest voor bekende Nederlandse locaties tegen de gegenereerde GeoJSON-datasets.
Draait zonder externe dependencies (pure Python ray-casting).
"""

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if ((y1 > lat) != (y2 > lat)) and (lon < (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-15) + x1):
            inside = not inside
    return inside


def point_in_polygon(lon: float, lat: float, coords: list) -> bool:
    if not coords or not point_in_ring(lon, lat, coords[0]):
        return False
    for hole in coords[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def point_in_geometry(lon: float, lat: float, geom: dict) -> bool:
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        return point_in_polygon(lon, lat, coords)
    if gtype == "MultiPolygon":
        return any(point_in_polygon(lon, lat, poly) for poly in coords)
    return False


def main() -> int:
    luchtruim = json.loads((DATA_DIR / "luchtruim_mini2se.geojson").read_text(encoding="utf-8"))
    n2000 = json.loads((DATA_DIR / "natura2000_slim.geojson").read_text(encoding="utf-8"))

    all_features = luchtruim.get("features", []) + n2000.get("features", [])

    # (naam, lat, lon, verwacht_minstens_een_forbidden)
    test_cases = [
        ("Schiphol CTR", 52.3105, 4.7683, True),
        ("Woensdrecht Militaire CTR", 51.4491, 4.3420, True),
        ("Oostvaardersplassen Natura 2000", 52.4480, 5.3600, True),
        ("Amersfoort (vrij luchtruim)", 52.1550, 5.3875, False),
    ]

    failures = 0
    for name, lat, lon, expect_forbidden in test_cases:
        hits = [
            f.get("properties", {})
            for f in all_features
            if f.get("geometry") and point_in_geometry(lon, lat, f["geometry"])
        ]
        has_forbidden = any(p.get("status") == "forbidden" for p in hits)
        if has_forbidden != expect_forbidden:
            print(f"FAIL: {name} ({lat}, {lon}) -> has_forbidden={has_forbidden}, verwacht={expect_forbidden}")
            failures += 1
        else:
            print(f"OK:   {name} ({len(hits)} zones, forbidden={has_forbidden})")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
