#!/usr/bin/env python3
"""
Regressietests voor de gegenereerde datasets in /data.

1. Integriteit: verplichte bestanden, minimale aantallen, geldige statussen, meta.json-bronnen.
2. Bekende Nederlandse locaties met verwachte uitkomst (verboden / let op / geen beperking).

Draait zonder externe dependencies (pure Python ray-casting) en wordt in GitHub Actions
uitgevoerd VÓÓR publicatie: faalt een test, dan wordt er niets gecommit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

VALID_STATUSES = {"forbidden", "caution", "free", "info"}
VALID_SOURCE_STATUSES = {"ok", "cached", "error"}


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
    return not any(point_in_ring(lon, lat, hole) for hole in coords[1:])


def point_in_geometry(lon: float, lat: float, geom: dict) -> bool:
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        return point_in_polygon(lon, lat, coords)
    if gtype == "MultiPolygon":
        return any(point_in_polygon(lon, lat, poly) for poly in coords)
    return False


def load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def effective_status(p: dict) -> str:
    """Status zoals de app hem voor A1/A2 toont (tijdgebonden zones: actieve status)."""
    if p.get("type") in ("GLV_LOW_FLY", "TIME_BOUND_AIRSPACE"):
        return p.get("status_active") or "caution"
    return p.get("status") or "forbidden"


def main() -> int:
    failures: List[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("OK:   " if cond else "FAIL: ") + msg)
        if not cond:
            failures.append(msg)

    # ---------- 1. Integriteit ----------
    luchtruim = load("luchtruim_mini2se.geojson").get("features", [])
    n2000 = load("natura2000_slim.geojson").get("features", [])
    notams = load("notams_actueel.geojson").get("features", [])
    meta = load("meta.json")

    check(len(luchtruim) >= 500, f"luchtruim bevat >= 500 zones ({len(luchtruim)})")
    check(len(n2000) >= 150, f"Natura 2000 bevat >= 150 gebieden ({len(n2000)})")
    check(sum(1 for f in luchtruim if f["properties"].get("type", "").startswith("CTR_ZONE")) >= 10,
          "gelaagde civiele CTR-zones aanwezig")
    check(sum(1 for f in luchtruim if f["properties"].get("type") == "GLV_LOW_FLY") >= 10,
          "laagvlieggebieden aanwezig")
    bad_status = [f["properties"].get("naam") for f in luchtruim + n2000
                  if f["properties"].get("status") not in VALID_STATUSES | {None}]
    check(not bad_status, f"alle statussen geldig ({bad_status[:3]})")
    check(all(f.get("geometry") for f in luchtruim + n2000 + notams), "alle features hebben geometrie")
    check(all(f["properties"].get("valid_from") and f["properties"].get("valid_to") for f in notams),
          "alle NOTAMs hebben valid_from/valid_to")
    check(not any(str(f["properties"].get("valid_to", "")).startswith("2027-12-31T23:59:59") for f in notams),
          "geen oude '2027-12-31' placeholder-eindtijden meer in NOTAMs")

    sources = meta.get("sources") or {}
    for key in ("godrone_luchtruim", "godrone_notams", "natura2000"):
        src = sources.get(key) or {}
        check(src.get("status") in VALID_SOURCE_STATUSES, f"meta.json bron '{key}' heeft geldige status ({src.get('status')})")
        check(bool(src.get("last_success")), f"meta.json bron '{key}' heeft last_success")

    # ---------- 2. Bekende locaties ----------
    all_features = luchtruim + n2000
    # (naam, lat, lon, verwacht hoogste status, verplicht zonetype of None)
    cases: List[Tuple[str, float, float, str, Optional[str]]] = [
        ("Schiphol CTR (kern)", 52.3105, 4.7683, "forbidden", None),
        ("Hoofddorp — Schiphol CTR zone 2A", 52.303, 4.690, "forbidden", "CTR_ZONE_2"),
        ("Haarlem — Schiphol CTR zone 3 (vrij)", 52.381, 4.636, "free", "CTR_ZONE_3"),
        ("Zoetermeer — Rotterdam CTR zone 3 (vrij)", 52.060, 4.494, "free", "CTR_ZONE_3"),
        ("Rotterdam Airport — CTR zone 1", 51.957, 4.437, "forbidden", "CTR_ZONE_1"),
        ("Lelystad Airport — CTR zone 1", 52.460, 5.527, "forbidden", "CTR_ZONE_1"),
        ("Groningen Eelde — CTR zone 1", 53.125, 6.583, "forbidden", "CTR_ZONE_1"),
        ("Woensdrecht militaire CTR", 51.4491, 4.3420, "forbidden", None),
        ("Paleis Noordeinde Den Haag", 52.0815, 4.3060, "forbidden", None),
        ("Oostvaardersplassen Natura 2000", 52.4480, 5.3600, "forbidden", None),
        ("Hoge Veluwe nationaal park", 52.070, 5.830, "forbidden", None),
        ("GLV II Midden-Drenthe (max 30m)", 52.902, 6.545, "caution", "GLV_LOW_FLY"),
        ("GLV VI Salland (max 30m)", 52.337, 6.350, "caution", "GLV_LOW_FLY"),
        ("Amersfoort (geen beperking)", 52.1550, 5.3875, "free", None),
        ("Utrecht centrum (geen beperking)", 52.090, 5.121, "free", None),
        ("Gouda stad (geen beperking)", 52.011, 4.711, "free", None),
        ("Assen (geen beperking)", 52.993, 6.562, "free", None),
    ]
    rank = {"free": 0, "info": 0, "caution": 1, "forbidden": 2}
    for name, lat, lon, expected, must_type in cases:
        hits = [f["properties"] for f in all_features if point_in_geometry(lon, lat, f["geometry"])]
        worst = max((effective_status(p) for p in hits), key=lambda s: rank.get(s, 2), default="free")
        if rank.get(worst) == 0:
            worst = "free"
        type_ok = must_type is None or any(p.get("type") == must_type for p in hits)
        check(worst == expected and type_ok,
              f"{name}: verwacht {expected}{' + ' + must_type if must_type else ''}, kreeg {worst} ({len(hits)} zones)")

    print(f"\n{len(failures)} fout(en)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
