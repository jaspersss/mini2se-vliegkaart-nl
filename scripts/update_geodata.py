#!/usr/bin/env python3
"""
update_geodata.py — Volledige Resolutie GeoJSON Pipeline + SHA-256 Versie-Manifest
===================================================================================
Dit script wordt uitgevoerd door GitHub Actions (.github/workflows/update_data.yml) en:
1. Haalt de volledige, ongecomprimeerde Natura 2000-gebieden (en Stiltegebieden) op via PDOK WFS.
2. Behoudt 100% van alle originele polygon-hoekpunten (geen Douglas-Peucker vervorming!)
   en rondt coördinaten enkel af op 5 decimalen (~1,1 meter GPS-precisie) om overbodige
   floating-point tekst-ruis te verwijderen.
3. Voert een Spatial Join uit tussen Natura 2000 en Stiltegebieden en koppelt
   `data/n2000_overrides.json` per gebiedsnummer.
4. Genereert `data/meta.json` met de SHA-256 hash en bestandsgrootte van elk GeoJSON-bestand.
   Hierdoor kan de browser (via IndexedDB) bij het openen van de app eerst `data/meta.json`
   (~250 bytes) controleren en een GeoJSON-bestand ALLEEN downloaden als de hash daadwerkelijk
   is veranderd!
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

try:
    from shapely.geometry import shape
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("update_geodata")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

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


def round_coords(coords: Any, precision: int = 5) -> Any:
    """Rondt coördinaten af op 5 decimalen (~1.1m nauwkeurigheid) zonder hoekpunten te verwijderen."""
    if isinstance(coords, (float, int)):
        return round(float(coords), precision)
    if isinstance(coords, (list, tuple)):
        return [round_coords(c, precision) for c in coords]
    return coords


def preserve_full_geometry(geom_dict: Dict[str, Any], precision: int = 5) -> Dict[str, Any]:
    """Behoudt alle originele vertices (100% ongecomprimeerde vorm) met 5-decimalen coördinaatprecisie."""
    geom_copy = dict(geom_dict)
    if "coordinates" in geom_copy:
        geom_copy["coordinates"] = round_coords(geom_copy["coordinates"], precision)
    return geom_copy


def fetch_json_url(url: str, timeout: int = 40) -> Optional[Dict[str, Any]]:
    """Haalt JSON/GeoJSON op van een publieke WFS/API."""
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


def save_geojson_if_changed(path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Slaat GeoJSON op zonder kwaliteitsverlies en berekent de SHA-256 hash.
    Schrijft het bestand alleen naar schijf als de inhoud (hash) daadwerkelijk is veranderd.
    """
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = serialized.encode("utf-8")
    new_hash = hashlib.sha256(encoded).hexdigest()[:16]
    size_kb = round(len(encoded) / 1024.0, 1)
    feature_count = len(data.get("features", []))

    old_hash = None
    if path.exists():
        old_bytes = path.read_bytes()
        old_hash = hashlib.sha256(old_bytes).hexdigest()[:16]

    if old_hash == new_hash:
        logger.info("Ongewijzigd: %s (hash=%s, %.1f KB, %d features)", path.name, new_hash, size_kb, feature_count)
    else:
        path.write_bytes(encoded)
        logger.info("Bijgewerkt: %s (hash=%s, %.1f KB, %d features)", path.name, new_hash, size_kb, feature_count)

    return {
        "hash": new_hash,
        "size_kb": size_kb,
        "features": feature_count,
    }


def update_stiltegebieden() -> Dict[str, Any]:
    """Haalt provinciale stiltegebieden op via PDOK of behoudt de lokale dataset op volledige resolutie."""
    target_path = DATA_DIR / "stiltegebieden.geojson"
    raw = fetch_json_url(PDOK_STILTEGEBIEDEN_WFS)

    if not raw or not raw.get("features"):
        logger.info("Gebruik bestaande stiltegebieden.geojson dataset.")
        existing = load_local_json(target_path)
        if existing.get("features"):
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
            "geometry": preserve_full_geometry(geom, precision=5),
        })

    return {"type": "FeatureCollection", "name": "stiltegebieden", "features": features}


def enrich_natura2000(stilte_fc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verrijkt Natura 2000-gebieden op 100% originele geometrie-resolutie (geen vereenvoudiging):
    1. PDOK Natura 2000 basisgeometrie (alle 209 gebieden in Nederland)
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
        logger.info("PDOK Natura 2000 WFS niet bereikbaar; gebruik lokale natura2000_slim.geojson.")
        raw_n2000 = load_local_json(target_path)

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
        naam = (
            props.get("naamN2K")
            or props.get("naam_n2k")
            or props.get("naam")
            or props.get("NAAM")
            or f"Natura 2000 #{gebiedsnummer}"
        )

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
        existing_status = props.get("status")
        if existing_status not in ("forbidden", "caution", "free"):
            existing_status = "caution"
        status = override.get("status", existing_status)
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
            "geometry": preserve_full_geometry(geom_dict, precision=5),
        })

    return {"type": "FeatureCollection", "name": "natura2000_slim", "features": enriched_features}


def main() -> None:
    logger.info("Start Mini2SE Vliegkaart NL GeoJSON data-pipeline (100%% ongecomprimeerde geometrie)...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    stilte_fc = update_stiltegebieden()
    n2000_fc = enrich_natura2000(stilte_fc)
    luchtruim_fc = load_local_json(DATA_DIR / "luchtruim_mini2se.geojson")
    notams_fc = load_local_json(DATA_DIR / "notams_actueel.geojson")

    manifest_files: Dict[str, Any] = {}
    manifest_files["stiltegebieden.geojson"] = save_geojson_if_changed(DATA_DIR / "stiltegebieden.geojson", stilte_fc)
    manifest_files["natura2000_slim.geojson"] = save_geojson_if_changed(DATA_DIR / "natura2000_slim.geojson", n2000_fc)
    if luchtruim_fc.get("features"):
        manifest_files["luchtruim_mini2se.geojson"] = save_geojson_if_changed(DATA_DIR / "luchtruim_mini2se.geojson", luchtruim_fc)
    if notams_fc.get("features"):
        manifest_files["notams_actueel.geojson"] = save_geojson_if_changed(DATA_DIR / "notams_actueel.geojson", notams_fc)

    # Schrijf data/meta.json alleen als een van de hashes is gewijzigd
    meta_path = DATA_DIR / "meta.json"
    meta_doc = {"files": manifest_files}
    meta_bytes = json.dumps(meta_doc, indent=2, sort_keys=True).encode("utf-8")
    if not meta_path.exists() or meta_path.read_bytes() != meta_bytes:
        meta_path.write_bytes(meta_bytes)
        logger.info("Versie-manifest data/meta.json bijgewerkt.")
    else:
        logger.info("Versie-manifest data/meta.json is ongewijzigd.")


if __name__ == "__main__":
    main()
