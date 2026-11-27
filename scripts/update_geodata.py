#!/usr/bin/env python3
"""
update_geodata.py — Geautomatiseerde GeoJSON Data-Pipeline voor Mini2SE Vliegkaart NL
=====================================================================================
Dit script wordt uitgevoerd door GitHub Actions (.github/workflows/update_data.yml) en:
1. Haalt actuele Natura 2000-gebieden en Provinciale Stiltegebieden op via PDOK WFS / OGC API.
2. Voert een Spatial Join (Shapely) uit tussen Natura 2000 en Stiltegebieden.
3. Koppelt de handmatige kennisbank `data/n2000_overrides.json` per `gebiedsnummer` voor
   een concreet DJI Mini 2 SE (< 250g, A1) oordeel ('forbidden', 'caution', 'free').
4. Vereenvoudigt alle geometrieën (Douglas-Peucker) en rondt coördinaten af op 4-5 decimalen
   zodat elk bestand in `/data/` onder de 500 KB blijft voor snelle mobiele laadtijden.
5. Bevat een veilige fallback: als een externe overheids-WFS tijdelijk niet reageert,
   worden bestaande valide GeoJSON-bestanden in `/data/` behouden en verrijkt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

try:
    from shapely.geometry import shape, mapping
    from shapely.strtree import STRtree
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("update_geodata")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# PDOK WFS endpoints (CRS: EPSG:4326 / WGS84)
PDOK_NATURA2000_WFS = (
    "https://service.pdok.nl/rvo/natura2000/wfs/v1_0"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=natura2000:natura2000&outputFormat=application/json"
    "&srsName=EPSG:4326&count=250"
)

PDOK_STILTEGEBIEDEN_WFS = (
    "https://service.pdok.nl/provincies/stiltegebieden/wfs/v1_0"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=stiltegebieden:stiltegebieden&outputFormat=application/json"
    "&srsName=EPSG:4326&count=250"
)


def round_coords(coords: Any, precision: int = 4) -> Any:
    """Rondt recursief alle coördinaten in een GeoJSON-structuur af om bestandsgrootte te beperken."""
    if isinstance(coords, (float, int)):
        return round(float(coords), precision)
    if isinstance(coords, (list, tuple)):
        return [round_coords(c, precision) for c in coords]
    return coords


def simplify_geometry(geom_dict: Dict[str, Any], tolerance: float = 0.0008, precision: int = 4) -> Dict[str, Any]:
    """Vereenvoudigt een GeoJSON-geometrie met Douglas-Peucker (Shapely) en rondt coördinaten af."""
    if HAS_SHAPELY:
        try:
            geom = shape(geom_dict)
            if not geom.is_valid:
                geom = geom.buffer(0)
            simplified = geom.simplify(tolerance, preserve_topology=True)
            out = mapping(simplified)
            out["coordinates"] = round_coords(out["coordinates"], precision)
            return out
        except Exception as exc:
            logger.warning("Shapely vereenvoudiging overgeslagen: %s", exc)

    geom_copy = dict(geom_dict)
    if "coordinates" in geom_copy:
        geom_copy["coordinates"] = round_coords(geom_copy["coordinates"], precision)
    return geom_copy


def fetch_json_url(url: str, timeout: int = 25) -> Optional[Dict[str, Any]]:
    """Haalt JSON/GeoJSON op van een publieke WFS/API met nette User-Agent en foutafhandeling."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mini2SE-Vliegkaart-NL/1.0 (+https://github.com/jaspersss/mini2se-vliegkaart-nl)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and "features" in data:
                    return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kon externe WFS niet ophalen (%s): %s", url.split("?")[0], exc)
    return None


def load_local_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_compact_geojson(path: Path, data: Dict[str, Any], max_kb: int = 490) -> None:
    """Slaat GeoJSON compact op en controleert of het bestand onder de 500 KB limiet blijft."""
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    size_kb = len(serialized.encode("utf-8")) / 1024.0

    # Als het bestand groter is dan max_kb, vereenvoudig met een iets grovere tolerantie
    if size_kb > max_kb and "features" in data:
        logger.info("Bestand %s is %.1f KB (> %d KB), extra Douglas-Peucker vereenvoudiging toepassen...", path.name, size_kb, max_kb)
        for feat in data["features"]:
            if feat.get("geometry"):
                feat["geometry"] = simplify_geometry(feat["geometry"], tolerance=0.0022, precision=4)
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        size_kb = len(serialized.encode("utf-8")) / 1024.0

    with path.open("w", encoding="utf-8") as f:
        f.write(serialized)
    logger.info("Opgeslagen: %s (%.1f KB, %d features)", path.name, size_kb, len(data.get("features", [])))


def update_stiltegebieden() -> Dict[str, Any]:
    """Haalt provinciale stiltegebieden op via PDOK of behoudt en optimaliseert de lokale dataset."""
    target_path = DATA_DIR / "stiltegebieden.geojson"
    raw = fetch_json_url(PDOK_STILTEGEBIEDEN_WFS)

    if not raw or not raw.get("features"):
        logger.info("Gebruik en optimaliseer bestaande stiltegebieden.geojson dataset.")
        existing = load_local_json(target_path)
        if existing.get("features"):
            save_compact_geojson(target_path, existing)
            return existing
        return {"type": "FeatureCollection", "name": "stiltegebieden", "features": []}

    features: List[Dict[str, Any]] = []
    for idx, feat in enumerate(raw.get("features", [])):
        geom = feat.get("geometry")
        if not geom:
            continue
        props = feat.get("properties") or {}
        naam = props.get("naam") or props.get("NAAM") or props.get("omschrijving") or f"Stiltegebied #{idx + 1}"
        provincie = props.get("provincie") or props.get("PROVINCIE") or "Provinciaal"

        features.append({
            "type": "Feature",
            "properties": {
                "id": str(props.get("identificatie") or f"STILTE_{idx + 1}"),
                "naam": str(naam),
                "provincie": str(provincie),
                "status": "forbidden",
                "max_height_m": 0,
                "mini2se_toelichting": (
                    f"Provinciaal Stiltegebied ({provincie}): het gebruik van gemotoriseerde "
                    "modelvliegtuigen en drones is in een stiltegebied verboden onder de "
                    "provinciale Omgevingsverordening."
                ),
            },
            "geometry": simplify_geometry(geom, tolerance=0.0010, precision=4),
        })

    out = {"type": "FeatureCollection", "name": "stiltegebieden", "features": features}
    save_compact_geojson(target_path, out)
    return out


def enrich_natura2000(stilte_fc: Dict[str, Any]) -> None:
    """
    Verrijkt Natura 2000-gebieden via 3 stappen:
    1. PDOK Natura 2000 basisgeometrie (of lokale basislaag als WFS niet bereikbaar is)
    2. Spatial Join met Provinciale Stiltegebieden -> `stiltegebied: true` (status 'forbidden')
    3. Koppeling met `data/n2000_overrides.json` per gebiedsnummer.
    """
    overrides_path = DATA_DIR / "n2000_overrides.json"
    target_path = DATA_DIR / "natura2000_slim.geojson"

    overrides_doc = load_local_json(overrides_path)
    overrides_map: Dict[str, Any] = overrides_doc.get("overrides", {})
    default_advice: str = overrides_doc.get("_meta", {}).get(
        "default_advice",
        "Geen expliciet droneverbod in besluit, maar verstoring van fauna is strafbaar. "
        "Houd >50-100m afstand van vogels en stijg niet op vanaf terrein van "
        "Staatsbosbeheer/Natuurmonumenten zonder toestemming.",
    )

    raw_n2000 = fetch_json_url(PDOK_NATURA2000_WFS)
    if not raw_n2000 or not raw_n2000.get("features"):
        logger.info("PDOK Natura 2000 WFS niet direct bereikbaar; verrijk bestaande natura2000_slim.geojson met nieuwste overrides.")
        raw_n2000 = load_local_json(target_path)

    # Bouw Shapely spatial index voor stiltegebieden
    stilte_geoms = []
    if HAS_SHAPELY:
        for sf in stilte_fc.get("features", []):
            if sf.get("geometry"):
                try:
                    g = shape(sf["geometry"])
                    if not g.is_valid:
                        g = g.buffer(0)
                    stilte_geoms.append(g)
                except Exception:
                    pass

    enriched_features: List[Dict[str, Any]] = []
    for idx, feat in enumerate(raw_n2000.get("features", [])):
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue

        props = feat.get("properties") or {}
        nr_raw = (
            props.get("gebiedsnummer")
            or props.get("GEBIEDSNUMMER")
            or props.get("nr")
            or str(idx + 1)
        )
        gebiedsnummer = str(nr_raw).lstrip("0") or str(nr_raw)
        naam = props.get("naam") or props.get("NAAM") or props.get("naam_n2k") or f"Natura 2000 #{gebiedsnummer}"

        # Spatial Join met Stiltegebieden
        overlaps_stilte = bool(props.get("stiltegebied", False))
        if HAS_SHAPELY and stilte_geoms:
            try:
                n2k_geom = shape(geom_dict)
                if not n2k_geom.is_valid:
                    n2k_geom = n2k_geom.buffer(0)
                for sg in stilte_geoms:
                    if n2k_geom.intersects(sg) and n2k_geom.intersection(sg).area > 1e-6:
                        overlaps_stilte = True
                        break
            except Exception:
                pass

        override = overrides_map.get(gebiedsnummer, {})
        status = override.get("status", props.get("status", "caution"))
        if overlaps_stilte:
            status = "forbidden"

        max_height = 0 if status == "forbidden" else int(override.get("max_height_m", 120))
        toelichting = override.get("mini2se_toelichting") or props.get("mini2se_toelichting") or default_advice
        if overlaps_stilte and "Stiltegebied" not in toelichting:
            toelichting = f"Overlapt met een Provinciaal Stiltegebied (verboden te vliegen). {toelichting}"

        url = override.get("url") or props.get("url") or "https://www.natura2000.nl/gebieden"
        beheerder = override.get("beheerder") or props.get("beheerder") or "Staatsbosbeheer / Natuurmonumenten / Provincie"

        enriched_features.append({
            "type": "Feature",
            "properties": {
                "gebiedsnummer": gebiedsnummer,
                "naam": str(override.get("naam") or naam),
                "status": status,
                "stiltegebied": overlaps_stilte,
                "max_height_m": max_height,
                "beheerder": beheerder,
                "mini2se_toelichting": toelichting,
                "url": url,
            },
            "geometry": simplify_geometry(geom_dict, tolerance=0.0010, precision=4),
        })

    out = {"type": "FeatureCollection", "name": "natura2000_slim", "features": enriched_features}
    save_compact_geojson(target_path, out)


def optimize_luchtruim_and_notams() -> None:
    """Valideert en comprimeert luchtruim_mini2se.geojson en notams_actueel.geojson (< 500 KB)."""
    for filename in ("luchtruim_mini2se.geojson", "notams_actueel.geojson"):
        path = DATA_DIR / filename
        data = load_local_json(path)
        if data.get("features"):
            for feat in data["features"]:
                if feat.get("geometry"):
                    feat["geometry"] = simplify_geometry(feat["geometry"], tolerance=0.0006, precision=4)
            save_compact_geojson(path, data)


def main() -> None:
    logger.info("Start Mini2SE Vliegkaart NL GeoJSON data-pipeline...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    stilte_fc = update_stiltegebieden()
    enrich_natura2000(stilte_fc)
    optimize_luchtruim_and_notams()

    logger.info("GeoJSON data-pipeline succesvol afgerond.")


if __name__ == "__main__":
    main()
