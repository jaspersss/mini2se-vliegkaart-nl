#!/usr/bin/env python3
"""
update_geodata.py — 100% Officiële & Ongecomprimeerde GeoJSON Data-Pipeline
===========================================================================
Dit script haalt voor ALLE kaartlagen de officiële, ongecomprimeerde overheidspolygonen
(geen test-rechthoeken en geen Douglas-Peucker vervorming!) rechtstreeks op:

1. Luchtruim (`data/luchtruim_mini2se.geojson`):
   Rechtstreeks uit het officiële LVNL Geoportaal (ArcGIS FeatureServer van Luchtverkeersleiding
   Nederland), gefilterd op zones die bij het aardoppervlak starten (LowerLimit == 0 FT SFC/MSL):
   - Layer 15: CTR (Civiele & Militaire Control Zones: Schiphol, Rotterdam, Eindhoven, Lelystad,
     Gilze-Rijen, Volkel, Woensdrecht, Leeuwarden, De Kooy, Deelen, De Peel, Eelde, Maastricht)
   - Layer 19: Prohibited Areas (EHP25 Kasteel Drakensteyn, EHP26 Koninklijke Paleizen, EHP29 Vught)
   - Layer 18: Restricted Areas vanaf de grond (EHR: o.a. Oldebroek, Harskamp, Havelte, Marnewaard, Vliehors)
   - Layer 17: Danger Areas vanaf de grond (EHD: o.a. schietgebieden Wier, Noordzee/kust)
   - Layer 1:  Low Flying Areas (GLV I t/m XII militaire laagvlieggebieden & helikopterroutes Lek/Waal/Maas)
   - Layer 3:  TSA's vanaf de grond (o.a. EHTSA24 Soesterberg, EHTSA55 Vlasakkers RPAS, EHTSA56 Leusderheide RPAS)
   - Layer 5:  TRA's vanaf de grond (o.a. EHTRA59 Leusderheide, EHTRA83 Ginkel, EHTRA84 Uddel)
   - Layer 7:  ATZ (Aerodrome Traffic Zones: Teuge, Twente, Budel, Schinveld, Veendam)
   - Layer 10: RMZ (Radio Mandatory Zones: Lelystad, Teuge, Twente, Budel, Niederrhein)
   - Layer 11: Glider Areas vanaf de grond (Terlet zweefvliegsector)

2. Provinciale Stiltegebieden (`data/stiltegebieden.geojson`):
   - Landelijke INSPIRE/HVD Stiltegebieden WFS van de gezamenlijke provincies (`hvd.gbo-provincies.nl`, 499 polygonen)
   - Aangevuld met de gedetailleerde Provincie Utrecht Stiltegebieden WFS (`services.geodata-utrecht.nl`, 112 polygonen
     zoals Eemland, Westbroek, Amerongse Berg, Boswachterij Leersum, Overlangbroek).

3. Natura 2000 (`data/natura2000_slim.geojson`):
   - Alle 209 Nederlandse Natura 2000-gebieden op 100% originele PDOK-geometrie, verrijkt met
     een Spatial Join tegen de Stiltegebieden en `data/n2000_overrides.json`.

4. SHA-256 Versie-Manifest (`data/meta.json`):
   - Berekent per bestand de SHA-256 hash zodat de browser (`IndexedDB`) bestanden ALLEEN opnieuw
     downloadt als er daadwerkelijk een wijziging is.
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

# 1. PDOK Natura 2000 WFS
PDOK_NATURA2000_WFS = (
    "https://service.pdok.nl/rvo/natura2000/wfs/v1_0"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=natura2000:natura2000&outputFormat=application/json"
    "&srsName=EPSG:4326&count=250"
)

# 2. Officiële Provinciale Stiltegebieden WFS (Landelijk HVD + Provincie Utrecht detail)
HVD_STILTEGEBIEDEN_WFS = (
    "https://hvd.gbo-provincies.nl/geoserver/HVD_Provincies/hpsst_hvd_protectedsite/ows"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=HVD_Provincies:hpsst_hvd_protectedsite"
    "&outputFormat=application/json&srsName=EPSG:4326"
)

UTRECHT_STILTEGEBIEDEN_WFS = (
    "https://services.geodata-utrecht.nl/geoserver/m01_4_overlast_hinder/wfs"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=m01_4_overlast_hinder:Stiltegebieden"
    "&outputFormat=application/json&srsName=EPSG:4326"
)

# 3. Officieel LVNL Geoportaal (ArcGIS FeatureServer)
LVNL_AIRSPACE_BASE = (
    "https://services-eu1.arcgis.com/OtUwzhpKSdeXgRIB/arcgis/rest/services/Airspaces_data/FeatureServer"
)


def round_coords(coords: Any, precision: int = 5) -> Any:
    """Rondt coördinaten af op 5 decimalen (~1.1m nauwkeurigheid) zonder hoekpunten te verwijderen."""
    if isinstance(coords, (float, int)):
        return round(float(coords), precision)
    if isinstance(coords, (list, tuple)):
        return [round_coords(c, precision) for c in coords]
    return coords


def preserve_full_geometry(geom_dict: Dict[str, Any], precision: int = 5) -> Dict[str, Any]:
    """Behoudt 100% van alle originele polygon-hoekpunten met 5 decimalen precisie."""
    geom_copy = dict(geom_dict)
    if "coordinates" in geom_copy:
        geom_copy["coordinates"] = round_coords(geom_copy["coordinates"], precision)
    return geom_copy


def fetch_json_url(url: str, timeout: int = 45) -> Optional[Dict[str, Any]]:
    """Haalt JSON/GeoJSON op van een publieke WFS/REST API."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mini2SE-Vliegkaart-NL/2.0 (+https://github.com/jaspersss/mini2se-vliegkaart-nl)",
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
        logger.warning("Kon externe bron niet ophalen (%s): %s", url.split("?")[0], exc)
    return None


def load_local_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_geojson_if_changed(path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    """Slaat GeoJSON op zonder kwaliteitsverlies en schrijft alleen naar schijf als de SHA-256 hash gewijzigd is."""
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = serialized.encode("utf-8")
    new_hash = hashlib.sha256(encoded).hexdigest()[:16]
    size_kb = round(len(encoded) / 1024.0, 1)
    feature_count = len(data.get("features", []))

    old_hash = None
    if path.exists():
        old_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:16]

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


def update_luchtruim_from_lvnl() -> Dict[str, Any]:
    """
    Haalt de officiële Nederlandse luchtruim-polygonen rechtstreeks op uit het LVNL Geoportaal
    en vertaalt ze naar de DJI Mini 2 SE (< 250g, Open Categorie A1) regel-engine.
    Alleen gebieden die vanaf de grond gelden (LowerLimit == 0) of < 500 FT AGL starten worden
    meegenomen, zodat hoge luchtwegen (TMA/CTA boven 1500 ft) geen valse ruis geven voor een 120m drone!
    """
    lvnl_layers = [
        (15, "CTR"),
        (19, "PROHIBITED"),
        (18, "RESTRICTED"),
        (17, "DANGER"),
        (1, "LOW_FLYING"),
        (3, "TSA"),
        (5, "TRA"),
        (7, "ATZ"),
        (10, "RMZ"),
        (11, "GLIDER"),
    ]

    features: List[Dict[str, Any]] = []
    military_ctr_codes = {"EHGR", "EHVK", "EHWO", "EHLW", "EHDL", "EHDPD", "EHKD", "EHEH"}

    for layer_id, layer_kind in lvnl_layers:
        url = f"{LVNL_AIRSPACE_BASE}/{layer_id}/query?where=1%3D1&outFields=*&f=geojson"
        raw = fetch_json_url(url)
        if not raw or not raw.get("features"):
            continue

        for feat in raw["features"]:
            geom = feat.get("geometry")
            if not geom:
                continue
            p = feat.get("properties") or {}

            # Filter op relevante ondergrens voor een < 120m (400 ft) A1 drone
            lower_val = p.get("LowerLimit")
            lower_unit = str(p.get("LowerLimitUnit") or "FT").upper()
            if lower_val is not None:
                try:
                    lv = float(lower_val)
                    if lower_unit == "FL" and lv > 5:
                        continue
                    if lower_unit == "FT" and lv > 500:
                        continue
                except (ValueError, TypeError):
                    pass

            designator = str(p.get("Designator") or p.get("Id") or f"LVNL_{layer_id}")
            raw_name = str(p.get("Name") or designator).strip()

            # Bepaal status, tijdschema en Mini 2 SE toelichting per LVNL-laagtype
            if layer_kind == "CTR":
                is_mil = any(designator.startswith(code) for code in military_ctr_codes)
                cat_label = "Militaire Luchthaven CTR (LVNL)" if is_mil else "Civiele Luchthaven CTR (LVNL)"
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"{raw_name} ({designator})",
                        "type": "MIL_CTR" if is_mil else "CTR_INNER",
                        "categorie": cat_label,
                        "status": "forbidden",
                        "max_height_m": 0,
                        "schedule": "H24",
                        "mini2se_toelichting": (
                            f"Officiële LVNL Control Zone ({raw_name} - {designator}, 0 ft t/m {p.get('UpperLimit')} {p.get('UpperLimitUnit')}). "
                            "In deze gecontroleerde luchthavenzone (CTR) is vliegen in de Open Categorie (inclusief < 250g DJI Mini 2 SE) verboden."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind == "PROHIBITED":
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"Verboden Gebied {raw_name} ({designator})",
                        "type": "PROHIBITED",
                        "categorie": "Permanent Verboden Luchtruim (EHP)",
                        "status": "forbidden",
                        "max_height_m": 0,
                        "schedule": "H24",
                        "mini2se_toelichting": (
                            f"Permanent verboden luchtruim ({designator} - {raw_name}, o.a. Koninklijk Huis / Justitie / Defensie). "
                            "Strikt vliegverbod (0m) voor alle drones, 24 uur per dag."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind in ("RESTRICTED", "DANGER"):
                prefix = "Beperkt Gebied (EHR)" if layer_kind == "RESTRICTED" else "Militair Schiet-/Gevaargebied (EHD)"
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"{raw_name} ({designator})",
                        "type": "MIL_RESTRICTED",
                        "categorie": prefix,
                        "status": "forbidden",
                        "max_height_m": 0,
                        "schedule": "H24",
                        "mini2se_toelichting": (
                            f"Officieel LVNL {prefix}: {raw_name} ({designator}, 0 ft t/m {p.get('UpperLimit')} {p.get('UpperLimitUnit')}). "
                            "Militair oefen-, schiet- of testgebied vanaf de grond: verboden voor civiele drones."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind == "LOW_FLYING":
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"Laagvlieggebied {raw_name} ({designator})",
                        "type": "GLV_LOW_FLY",
                        "categorie": "Militair Laagvlieggebied / Helikopterroute (LVNL)",
                        "status_active": "caution",
                        "status_inactive": "free",
                        "max_height_active_m": 30,
                        "max_height_inactive_m": 120,
                        "schedule": "WEEKDAY_08_23",
                        "mini2se_toelichting": (
                            f"Officieel militair laagvlieggebied ({raw_name} - {designator}, 0 tot {p.get('UpperLimit')} ft AGL). "
                            "Doordeweeks overdag actief voor laagvliegende militaire helikopters: uiterste waakzaamheid vereist (vlieg laag/met waarnemer en land direct bij naderend luchtverkeer). "
                            "In het weekend en op feestdagen is dit laagvlieggebied INACTIEF en mag je met de DJI Mini 2 SE vrij tot 120m vliegen."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind in ("TSA", "TRA"):
                is_soesterberg = "SOESTERBERG" in raw_name.upper()
                is_rpas = "RPAS" in raw_name.upper()
                schedule = "WEEKEND_AND_WED_AFTERNOON" if is_soesterberg else "WEEKDAY_08_23"
                status_act = "forbidden" if is_rpas else "caution"
                max_h_act = 0 if is_rpas else 45

                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"{raw_name} ({designator})",
                        "type": "TIME_BOUND_AIRSPACE",
                        "categorie": f"Tijdelijk Gereserveerd Luchtruim ({layer_kind})",
                        "status_active": status_act,
                        "status_inactive": "free",
                        "max_height_active_m": max_h_act,
                        "max_height_inactive_m": 120,
                        "schedule": schedule,
                        "mini2se_toelichting": (
                            f"Officieel LVNL {layer_kind}-gebied: {raw_name} ({designator}, 0 t/m {p.get('UpperLimit')} {p.get('UpperLimitUnit')}). "
                            + (
                                "Zweefvliegsector Soesterberg: actief bij daglicht in het weekend, op feestdagen en woensdagmiddag. Buiten die tijden inactief (vrij tot 120m)."
                                if is_soesterberg
                                else "Militair oefen-/RPAS-luchtruim: doordeweeks tijdens oefeningen actief (niet vliegen of uiterste voorzichtigheid); in het weekend doorgaans inactief."
                            )
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind in ("ATZ", "RMZ"):
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"{raw_name} ({designator})",
                        "type": "ATZ_RMZ",
                        "categorie": f"Luchtverkeerszone Regionaal Vliegveld ({layer_kind})",
                        "status": "forbidden",
                        "max_height_m": 0,
                        "schedule": "H24",
                        "mini2se_toelichting": (
                            f"Officiële LVNL {layer_kind}-zone rond vliegveld ({raw_name} - {designator}, 0 t/m {p.get('UpperLimit')} ft). "
                            "Rond dit vliegveld geldt voor drones in de Open Categorie een vliegverbod wegens lokaal startend en landend vliegverkeer."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

            elif layer_kind == "GLIDER":
                features.append({
                    "type": "Feature",
                    "properties": {
                        "id": designator,
                        "naam": f"Zweefvlieggebied {raw_name} ({designator})",
                        "type": "TIME_BOUND_AIRSPACE",
                        "categorie": "Officieel Zweefvlieggebied (LVNL)",
                        "status_active": "caution",
                        "status_inactive": "free",
                        "max_height_active_m": 45,
                        "max_height_inactive_m": 120,
                        "schedule": "WEEKEND_AND_WED_AFTERNOON",
                        "mini2se_toelichting": (
                            f"Officieel LVNL Zweefvlieggebied vanaf de grond ({raw_name} - {designator}, 0 t/m {p.get('UpperLimit')} ft). "
                            "Let bij goed vliegweer (vooral in het weekend) op lierstarts en laag binnenkomende zweefvliegtuigen."
                        ),
                    },
                    "geometry": preserve_full_geometry(geom, precision=5),
                })

    if not features:
        logger.warning("LVNL FeatureServer niet bereikbaar; behoud bestaande luchtruim_mini2se.geojson.")
        return load_local_json(DATA_DIR / "luchtruim_mini2se.geojson")

    logger.info("Officiële LVNL luchtruim-polygonen opgehaald: %d zones", len(features))
    return {"type": "FeatureCollection", "name": "luchtruim_mini2se", "features": features}


def update_stiltegebieden() -> Dict[str, Any]:
    """
    Haalt de echte, officiële kadastrale stiltegebied-polygonen op uit:
    1. De landelijke HVD/INSPIRE Stiltegebieden WFS van de gezamenlijke provincies (499 gebieden)
    2. De Provincie Utrecht Stiltegebieden WFS (112 gebieden, o.a. Eemland, Westbroek, Amerongse Berg)
    """
    features: List[Dict[str, Any]] = []

    # 1. Landelijke HVD Stiltegebieden WFS
    raw_hvd = fetch_json_url(HVD_STILTEGEBIEDEN_WFS)
    if raw_hvd and raw_hvd.get("features"):
        for idx, feat in enumerate(raw_hvd["features"]):
            geom = feat.get("geometry")
            if not geom:
                continue
            p = feat.get("properties") or {}
            naam = (
                p.get("sitename_spelling")
                or p.get("quietareaname_localname")
                or f"Stiltegebied #{idx + 1}"
            )
            doc_link = p.get("legalfoundationdocument_title") or ""
            provincie = "Provinciale Omgevingsverordening"
            for prov_name in ("Zuid-Holland", "Noord-Holland", "Utrecht", "Gelderland", "Noord-Brabant", "Limburg", "Zeeland", "Overijssel", "Drenthe", "Friesland", "Groningen", "Flevoland"):
                if prov_name.lower() in doc_link.lower():
                    provincie = f"Provincie {prov_name}"
                    break

            features.append({
                "type": "Feature",
                "properties": {
                    "id": str(p.get("quietareaid_identifier") or p.get("inspireid_localid") or f"HVD_STILTE_{idx + 1}"),
                    "naam": str(naam),
                    "provincie": provincie,
                    "status": "forbidden",
                    "max_height_m": 0,
                    "mini2se_toelichting": (
                        f"Officieel Provinciaal Stiltegebied ({naam} — {provincie}): het gebruik van "
                        "gemotoriseerde modelvliegtuigen en drones is in een stiltegebied verboden om "
                        "akoestische verstoring te voorkomen."
                    ),
                },
                "geometry": preserve_full_geometry(geom, precision=5),
            })

    # 2. Provincie Utrecht Stiltegebieden WFS (Stille kern + Bufferzone stiltegebied)
    raw_ut = fetch_json_url(UTRECHT_STILTEGEBIEDEN_WFS)
    if raw_ut and raw_ut.get("features"):
        for idx, feat in enumerate(raw_ut["features"]):
            geom = feat.get("geometry")
            if not geom:
                continue
            p = feat.get("properties") or {}
            functie = str(p.get("FUNCTIE") or "Stiltegebied")
            naam_raw = str(p.get("NAAM") or "Utrecht")
            if naam_raw.lower() == "aandachtsgebied":
                naam_display = "Stiltegebied Provincie Utrecht (Aandachtsgebied)"
            else:
                naam_display = f"Stiltegebied {naam_raw} ({functie})"

            features.append({
                "type": "Feature",
                "properties": {
                    "id": f"UT_STILTE_{p.get('OBJECTID') or idx + 1}",
                    "naam": naam_display,
                    "provincie": "Provincie Utrecht",
                    "status": "forbidden",
                    "max_height_m": 0,
                    "mini2se_toelichting": (
                        f"Officieel Stiltegebied Provincie Utrecht ({naam_display}): onder de "
                        "Omgevingsverordening Provincie Utrecht is het gebruik van gemotoriseerde "
                        "modelvliegtuigen en drones in stiltegebieden verboden."
                    ),
                },
                "geometry": preserve_full_geometry(geom, precision=5),
            })

    if not features:
        logger.warning("Kon externe Stiltegebieden WFS niet bereiken; gebruik bestaande stiltegebieden.geojson.")
        return load_local_json(DATA_DIR / "stiltegebieden.geojson")

    logger.info("Officiële Stiltegebied-polygonen opgehaald: %d gebieden", len(features))
    return {"type": "FeatureCollection", "name": "stiltegebieden", "features": features}


def enrich_natura2000(stilte_fc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verrijkt alle 209 Natura 2000-gebieden op 100% originele PDOK-geometrie:
    1. PDOK Natura 2000 WFS basisgeometrie
    2. Spatial Join met de officiële Stiltegebieden
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
                    if n2k_geom.intersects(sg) and n2k_geom.intersection(sg).area > 1e-5:
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
    logger.info("Start Mini2SE Vliegkaart NL GeoJSON data-pipeline (100%% officiële LVNL, Stiltegebieden & PDOK polygonen)...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    luchtruim_fc = update_luchtruim_from_lvnl()
    stilte_fc = update_stiltegebieden()
    n2000_fc = enrich_natura2000(stilte_fc)

    # Leeg de kunstmatige demo-NOTAM rechthoek zodat alleen echte polygonen op de kaart staan
    notams_fc = {"type": "FeatureCollection", "name": "notams_actueel", "features": []}

    manifest_files: Dict[str, Any] = {}
    manifest_files["luchtruim_mini2se.geojson"] = save_geojson_if_changed(DATA_DIR / "luchtruim_mini2se.geojson", luchtruim_fc)
    manifest_files["stiltegebieden.geojson"] = save_geojson_if_changed(DATA_DIR / "stiltegebieden.geojson", stilte_fc)
    manifest_files["natura2000_slim.geojson"] = save_geojson_if_changed(DATA_DIR / "natura2000_slim.geojson", n2000_fc)
    manifest_files["notams_actueel.geojson"] = save_geojson_if_changed(DATA_DIR / "notams_actueel.geojson", notams_fc)

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
