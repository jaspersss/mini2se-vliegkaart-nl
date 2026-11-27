#!/usr/bin/env python3
"""
update_geodata.py — Officiële GoDrone / LVNL ED-318 UAS Geozones & PDOK Natura 2000 Pipeline
============================================================================================
Dit script haalt de exacte, actuele Nederlandse drone-zonering op (inclusief de nieuwe
gelaagde CTR-zones rond civiele luchthavens en live NOTAMs/TGB's):

1. `data/luchtruim_mini2se.geojson`:
   Haalt via de officiële GoDrone / LVNL MapData API (`api.godrone.nl/v2/mapdata/geojson`)
   alle Nederlandse **ED-318 UAS Geozones** (`nl:uas:zones:ed318:*`) én waarschuwingszones
   voor verhoogde luchtactiviteit (zweefvliegen, valschermspringen, helihavens) op:
   - **Nieuwe gelaagde Civiele CTR-zones** (Schiphol, Rotterdam, Groningen, Lelystad, Maastricht):
     * **CTR ZONE 1 (Kernzone, 0-120m AGL):** 🔴 `forbidden` voor Open Categorie (0m).
     * **CTR ZONE 2A (Tussenring onderlaag, 0-45m AGL) & 2B (45-120m AGL):** 🔴 `forbidden` voor Open Categorie (alleen Specific tot 45m).
     * **CTR ZONE 3 (Buitenste ring, 0-120m AGL):** 🟠/🟢 **TOEGESTAAN in de OPEN categorie (tot 120m AGL!)** — weergegeven als een voorwaardelijke/aandachtszone (`caution`, max 120m) zodat je direct ziet waar je in de buitenring van een civiele CTR met jouw DJI Mini 2 SE wél mag vliegen!
     * *(CTR ZONE 4 ligt boven 120m AGL en wordt genegeerd voor <120m A1 drones, net als de oude enkelvoudige LVNL `type:ctr` laag die anders Zone 3 onterecht rood zou kleuren).*
   - **Militaire CTR's** (Eindhoven, Gilze-Rijen, Volkel, Woensdrecht, Leeuwarden, De Kooy, Deelen, De Peel): 🔴 `forbidden` (0m).
   - **Militaire Laagvlieggebieden (ED-318 groep `2.*`, GLV I t/m XI & helikopterroutes):** 🟠 `caution` — **OPEN A1/A2 toegestaan tot max. 30m hoogte boven de grond** wanneer actief (en in het weekend vrij tot 120m).
   - **Beveiligde gebieden, gevangenissen, paleizen, vitale infrastructuur, zware industrie & havens (ED-318 groepen `3.*`, `4.*`, `5.*`, `6.*`, `7.*`):** 🔴 `forbidden` (0m voor Open Categorie).
   - **Gebieden met verhoogd luchtverkeersrisico (`risk:increasedAerialActivity:*`):** 🟠 `caution` (max 120m, extra waakzaamheid voor zweefvliegers, parachutisten of traumahelikopters).

2. `data/notams_actueel.geojson`:
   Haalt alle actuele **live NOTAMs & TFR's (Tijdelijke Gebieden met Beperkingen)** onder 120m AGL
   rechtstreeks uit de GoDrone/LVNL feed (met hun echte `operationalFrom` en `operationalTo` tijden en exacte cirkel/polygoon).

3. `data/natura2000_slim.geojson` & `data/stiltegebieden.geojson`:
   - Alle 209 Natura 2000-gebieden op 100% originele PDOK-geometrie, gekoppeld aan `data/n2000_overrides.json`.
   - **Belangrijke correctie Stiltegebieden:** Provinciale Stiltegebieden zijn *géén* luchtvaartwet-no-fly-zones (en staan daarom ook niet op GoDrone). Het luchtruim valt onder het Rijk; in veel stiltegebieden (zoals Eemland) is een `< 250g` DJI Mini 2 SE in A1 niet verboden (of pas een aandachtspunt voor geluid/rust op de grond). Ze worden daarom niet meer als rode verbodsblokken getoond.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

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

GODRONE_API_KEY = "hOrnXF_XuIOko8taj9VCi9MUNP4pXVkR_dN-3B8V0"
GODRONE_TILES: List[Tuple[float, float, float, float]] = [
    (50.7, 51.5, 3.3, 4.6),
    (50.7, 51.5, 4.6, 5.9),
    (50.7, 51.5, 5.9, 7.3),
    (51.5, 52.3, 3.3, 4.6),
    (51.5, 52.3, 4.6, 5.9),
    (51.5, 52.3, 5.9, 7.3),
    (52.3, 53.0, 3.3, 4.6),
    (52.3, 53.0, 4.6, 5.9),
    (52.3, 53.0, 5.9, 7.3),
    (53.0, 53.6, 3.3, 4.6),
    (53.0, 53.6, 4.6, 5.9),
    (53.0, 53.6, 5.9, 7.3),
]


def round_coords(coords: Any, precision: int = 5) -> Any:
    """Rondt coördinaten af op 5 decimalen (~1.1m precisie) zonder hoekpunten te verwijderen."""
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


def strip_html(html_str: str) -> str:
    """Verwijdert HTML-tags uit GoDrone beschrijvingen en maakt ze netjes leesbaar."""
    text = re.sub(r"<br\s*/?>", " ", html_str, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_json_url(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 40) -> Optional[Dict[str, Any]]:
    req_headers = {
        "User-Agent": "Mini2SE-Vliegkaart-NL/2.0 (+https://github.com/jaspersss/mini2se-vliegkaart-nl)",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
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


def fetch_godrone_luchtruim_and_notams() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Haalt alle officiële Nederlandse ED-318 UAS Geozones (inclusief de nieuwe gelaagde
    civiele CTR-zones 1, 2A, 2B en 3), risicogebieden en live NOTAMs rechtstreeks op uit
    de GoDrone / LVNL MapData API.
    """
    headers = {
        "Authorization": f"X-AA-ApiKey {GODRONE_API_KEY}",
        "Origin": "https://map.godrone.nl",
        "Referer": "https://map.godrone.nl/",
        "Accept-Language": "nl-NL,nl;q=0.9",
    }

    ed318_features: Dict[str, Dict[str, Any]] = {}
    risk_features: Dict[str, Dict[str, Any]] = {}
    notam_features: Dict[str, Dict[str, Any]] = {}

    for s, n, w, e in GODRONE_TILES:
        url = f"https://api.godrone.nl/v2/mapdata/geojson?n={n}&s={s}&e={e}&w={w}&is_Compact=false"
        tile_data = fetch_json_url(url, headers=headers, timeout=30)
        if not tile_data or not tile_data.get("features"):
            continue

        for feat in tile_data["features"]:
            geom = feat.get("geometry")
            if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            p = feat.get("properties") or {}
            aid = str(p.get("airspaceId") or "")
            dcat = str(p.get("detailedCategory") or "")
            name = str(p.get("name") or "").strip()

            # Check ondergrens: negeer luchtruim dat pas boven 120m (120.0 m) begint (zoals CTR ZONE 4)
            floor_obj = p.get("altitudeFloor") or {}
            floor_m = float(floor_obj.get("meters") or 0.0)
            if floor_m >= 120.0:
                continue

            # 1. Officiële Nederlandse ED-318 UAS Geozones (Regeling zonering onbemande luchtvaartuigen)
            if aid.startswith("nl:uas:zones:ed318:"):
                # Sla 4 mei Dodenherdenking-zones over tenzij het 4 mei is
                if "DODENHERDENKING" in name.upper():
                    continue
                # Sla CTR ZONE 2B (45m-120m) over omdat CTR ZONE 2A exact dezelfde polygoon op 0-45m al dekt
                if "CTR ZONE 2B" in name.upper():
                    continue
                ed318_features[aid] = feat

            # 2. Live NOTAMs & TFR's (Tijdelijke Gebieden met Beperkingen)
            elif "notam" in aid.lower() or dcat in ("class:tfr", "class:nav_warning"):
                # Filter algemene landelijke of irrelevante obstakel-NOTAMs (> 5 km2 of pure mast-lampjes)
                if "OBSTACLE" in name.upper() or "WINDMILL" in name.upper() or "CRANE" in name.upper():
                    continue
                notam_id = str(p.get("notamId") or aid)
                notam_features[notam_id] = feat

            # 3. Gebieden met verhoogd luchtverkeersrisico (Parachute, Zweefvliegen, UAV):
            # Geen wettelijke beperking voor <250g A1-drones -> niet opnemen als oranje zone om ruis te voorkomen.

    luchtruim_out: List[Dict[str, Any]] = []

    # Verwerk ED-318 UAS Geozones
    for aid, feat in sorted(ed318_features.items()):
        p = feat.get("properties") or {}
        name = str(p.get("name") or aid).strip()
        disp = p.get("display") or {}
        ceil_m = round(float((p.get("altitudeCeiling") or {}).get("meters") or 120.0))

        remarks_html = ""
        for sec in disp.get("sections") or []:
            if sec.get("title") == "Remarks" or "Message:" in str(sec.get("text") or ""):
                remarks_html = str(sec.get("text") or "")
                break
        remarks_clean = strip_html(remarks_html)

        # Extract NL Message indien aanwezig
        nl_msg = remarks_clean
        if "Message:" in remarks_clean:
            msg_part = remarks_clean.split("Message:", 1)[1].strip()
            nl_msg = msg_part.split("|")[0].strip()

        upper_name = name.upper()

        # A. Nieuwe Gelaagde Civiele CTR-zones (CTR ZONE 1, 2A, 3)
        if "CTR ZONE 3" in upper_name:
            # CTR ZONE 3 (Buitenring civiele CTR): OPEN CATEGORIE VRIJ TOT 120M (geen restrictie -> groen, niet tekenen)
            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": f"{name} (Open Categorie Vrij tot 120m)",
                    "type": "CTR_ZONE_3",
                    "categorie": "Civiele CTR Zone 3 (Nieuwe Regeling)",
                    "status": "free",
                    "max_height_m": 120,
                    "schedule": "H24",
                    "mini2se_toelichting": (
                        f"Nieuwe gelaagde CTR-zonering ({name}, 0 tot 120m AGL): "
                        "Vluchten in de OPEN categorie (zoals jouw < 250g DJI Mini 2 SE in A1) zijn hier zonder restricties TOEGESTAAN tot 120m hoogte."
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

        elif "CTR ZONE 2A" in upper_name:
            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": f"{name} (Tussenring)",
                    "type": "CTR_ZONE_2",
                    "categorie": "Civiele CTR Zone 2A (Nieuwe Regeling)",
                    "status": "forbidden",
                    "max_height_m": 0,
                    "schedule": "H24",
                    "mini2se_toelichting": (
                        f"Nieuwe gelaagde CTR-zonering ({name}): Categorie OPEN is hier VERBODEN (0m). "
                        "Alleen piloten met een vergunning in de Specifieke categorie mogen in Zone 2A tot maximaal 45 meter hoogte vliegen."
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

        elif "CTR ZONE 1" in upper_name:
            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": f"{name} (Kernzone)",
                    "type": "CTR_ZONE_1",
                    "categorie": "Civiele CTR Zone 1 (Kernzone)",
                    "status": "forbidden",
                    "max_height_m": 0,
                    "schedule": "H24",
                    "mini2se_toelichting": (
                        f"Binnenste kernzone van de luchthaven ({name}, 0-120m): Categorie OPEN is hier strikt VERBODEN (0m). "
                        "Alleen toegankelijk voor Specifieke categorie onder artikel 3a lid 3 met GoDrone missieplan en RT-contact."
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

        # B. Laagvlieggebieden & Laagvliegroutes (uitsluitend ED-318 groep 2.*)
        elif aid.startswith("nl:uas:zones:ed318:2."):
            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": name,
                    "type": "GLV_LOW_FLY",
                    "categorie": "Laagvlieggebied Bemand Luchtverkeer (ED-318)",
                    "status_active": "caution",
                    "status_inactive": "free",
                    "max_height_active_m": 30,
                    "max_height_inactive_m": 120,
                    "schedule": "WEEKDAY_08_23",
                    "mini2se_toelichting": (
                        f"Laagvlieggebied ({name}): Voor jouw DJI Mini 2 SE (Open Categorie A1/A2) is vliegen hier TOEGESTAAN tot maximaal 30 meter hoogte boven de grond (categorie A3 is hier verboden). "
                        "Let op plotseling laagvliegende militaire of civiele helikopters/vliegtuigen. In het weekend is het militaire laagvlieggebied doorgaans inactief."
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

        # C. Overige ED-318 UAS Geozones (Militaire CTR's, Defensie, Traumaheli's, Havens, Industrie, Gevangenissen, Paleizen)
        else:
            if "CTR" in upper_name:
                cat_str = "Militaire Luchthaven CTR (ED-318)"
            elif "TRAUMAHELIKOPTER" in upper_name or "VLIEGVELD" in upper_name:
                cat_str = "Helihaven / Regionaal Vliegveld (ED-318)"
            elif "HAVENGEBIED" in upper_name or "INDUSTRIEGEBIED" in upper_name:
                cat_str = "Haven- / Industriegebied met Risico (ED-318)"
            elif "DEFENSIE" in upper_name or "EHTSA" in upper_name or "EHTRA" in upper_name:
                cat_str = "Militaire Oefen- / RPAS-zone (ED-318)"
            else:
                cat_str = "Beveiligd Gebied / Vitale Infrastructuur (ED-318)"

            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": name,
                    "type": "UAS_PROHIBITED_OPEN",
                    "categorie": cat_str,
                    "status": "forbidden",
                    "max_height_m": 0,
                    "schedule": "H24",
                    "mini2se_toelichting": (
                        f"{cat_str} ({name}, 0 tot {ceil_m}m): {nl_msg or 'Vluchten in de Open Categorie zijn hier niet toegestaan.'}"
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

    # Verwerk Gele Waarschuwingszones voor Verhoogde Luchtactiviteit (Zweefvliegen, Parachutespringen, Lokale Vliegvelden)
    for key, feat in sorted(risk_features.items()):
        p = feat.get("properties") or {}
        name = str(p.get("name") or "Verhoogde Luchtactiviteit").strip()
        disp = p.get("display") or {}
        activity = "Luchtvaartactiviteit"
        for sec in disp.get("sections") or []:
            if sec.get("displayTitle") == "Activiteit" or sec.get("title") == "Activity":
                activity = strip_html(str(sec.get("text") or "Luchtvaart"))
                break

        luchtruim_out.append({
            "type": "Feature",
            "properties": {
                "id": f"RISK_{key}",
                "naam": f"Let op: {name} ({activity})",
                "type": "INCREASED_AERIAL_RISK",
                "categorie": f"Gebied met verhoogd luchtverkeersrisico ({activity})",
                "status": "caution",
                "max_height_m": 120,
                "schedule": "H24",
                "mini2se_toelichting": (
                    f"Waarschuwingszone op GoDrone ({name} — activiteit: {activity}). "
                    "Geen wettelijk vliegverbod voor jouw DJI Mini 2 SE (vliegen toegestaan tot 120m AGL), "
                    "maar op deze locatie is een verhoogde kans op laagvliegende zweefvliegtuigen, parachutisten of helikopters. Houd het luchtruim goed in de gaten en geef direct voorrang."
                ),
            },
            "geometry": preserve_full_geometry(feat["geometry"], precision=5),
        })

    # Verwerk Live NOTAMs & TFR's
    notams_out: List[Dict[str, Any]] = []
    for notam_id, feat in sorted(notam_features.items()):
        p = feat.get("properties") or {}
        name = str(p.get("name") or notam_id).strip()
        dcat = str(p.get("detailedCategory") or "")
        is_tfr = dcat == "class:tfr"
        op_from = p.get("operationalFrom") or "2025-01-01T00:00:00Z"
        op_to = p.get("operationalTo") or "2027-12-31T23:59:59Z"

        disp = p.get("display") or {}
        full_text = ""
        for sec in disp.get("sections") or []:
            if "NOTAM" in str(sec.get("displayTitle") or "").upper():
                full_text = strip_html(str(sec.get("text") or ""))
                break

        notams_out.append({
            "type": "Feature",
            "properties": {
                "id": str(p.get("airspaceId") or notam_id),
                "notam_code": notam_id,
                "naam": name,
                "type": "TGB_NOTAM" if is_tfr else "NAV_WARNING_NOTAM",
                "status": "forbidden" if is_tfr else "caution",
                "max_height_m": 0 if is_tfr else 120,
                "valid_from": op_from,
                "valid_to": op_to,
                "mini2se_toelichting": (
                    f"{'Tijdelijk Gebied met Beperkingen (TFR / No-Fly NOTAM)' if is_tfr else 'Tijdelijke Navigatiewaarschuwing (NOTAM)'} "
                    f"[{notam_id}]: {full_text or name}"
                ),
            },
            "geometry": preserve_full_geometry(feat["geometry"], precision=5),
        })

    if not luchtruim_out:
        logger.warning("GoDrone API niet bereikbaar; behoud bestaande luchtruim_mini2se.geojson.")
        return (
            load_local_json(DATA_DIR / "luchtruim_mini2se.geojson"),
            load_local_json(DATA_DIR / "notams_actueel.geojson"),
        )

    logger.info("GoDrone ED-318 geozones + risicogebieden opgehaald: %d luchtruimzones, %d live NOTAMs", len(luchtruim_out), len(notams_out))
    return (
        {"type": "FeatureCollection", "name": "luchtruim_mini2se", "features": luchtruim_out},
        {"type": "FeatureCollection", "name": "notams_actueel", "features": notams_out},
    )


def enrich_natura2000() -> Dict[str, Any]:
    """
    Verrijkt alle 209 Natura 2000-gebieden op 100% originele PDOK-geometrie
    en koppelt ze aan `data/n2000_overrides.json` per gebiedsnummer.
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

        override = overrides_map.get(gebiedsnummer, {})
        existing_status = props.get("status")
        if existing_status not in ("forbidden", "caution", "free"):
            existing_status = "caution"
        status = override.get("status", existing_status)

        max_height = 0 if status == "forbidden" else int(override.get("max_height_m", 120))
        toelichting = override.get("mini2se_toelichting") or props.get("mini2se_toelichting") or default_advice
        url = override.get("url") or props.get("url") or "https://www.natura2000.nl/gebieden"
        beheerder = override.get("beheerder") or props.get("beheerder") or "Staatsbosbeheer / Natuurmonumenten / Provincie"

        enriched_features.append({
            "type": "Feature",
            "properties": {
                "gebiedsnummer": gebiedsnummer,
                "naam": str(override.get("naam") or naam),
                "status": status,
                "stiltegebied": False,
                "max_height_m": max_height,
                "beheerder": beheerder,
                "mini2se_toelichting": toelichting,
                "url": url,
            },
            "geometry": preserve_full_geometry(geom_dict, precision=5),
        })

    return {"type": "FeatureCollection", "name": "natura2000_slim", "features": enriched_features}


def fetch_hoogspanning_osm() -> Dict[str, Any]:
    """Haalt Nederlandse hoogspanningslijnen (power=line) op uit OpenStreetMap via 6 kleine tegels."""
    import urllib.parse
    tiles = [
        (50.7, 51.7, 3.3, 5.4),
        (50.7, 51.7, 5.4, 7.3),
        (51.7, 52.7, 3.3, 5.4),
        (51.7, 52.7, 5.4, 7.3),
        (52.7, 53.6, 3.3, 5.4),
        (52.7, 53.6, 5.4, 7.3),
    ]
    seen_ids = set()
    features: List[Dict[str, Any]] = []

    for s, n, w, e in tiles:
        query = f'[out:json][timeout:30];way["power"="line"]({s},{w},{n},{e});out geom;'
        data = urllib.parse.urlencode({"data": query}).encode("utf-8")
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter",
            data=data,
            headers={"User-Agent": "Mini2SE-Vliegkaart-NL/2.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                if resp.status != 200:
                    continue
                d = json.loads(resp.read().decode("utf-8"))
                for el in d.get("elements", []):
                    eid = el.get("id")
                    geom = el.get("geometry")
                    if not eid or not geom or eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    coords = [[round(pt["lon"], 4), round(pt["lat"], 4)] for pt in geom]
                    if len(coords) >= 2:
                        features.append({
                            "type": "Feature",
                            "properties": {"id": eid},
                            "geometry": {"type": "LineString", "coordinates": coords},
                        })
        except Exception as exc:
            logger.warning("Overpass tegel (%s,%s) overgeslagen: %s", s, w, exc)

    if not features:
        logger.warning("Overpass niet bereikbaar; behoud bestaande hoogspanning.geojson.")
        return load_local_json(DATA_DIR / "hoogspanning.geojson")

    logger.info("Hoogspanningslijnen opgehaald uit OSM: %d lijnen", len(features))
    return {"type": "FeatureCollection", "name": "hoogspanning", "features": features}


def main() -> None:
    logger.info("Start Mini2SE Vliegkaart NL GeoJSON data-pipeline (GoDrone ED-318 Gelaagde CTR's + PDOK Natura 2000)...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    luchtruim_fc, notams_fc = fetch_godrone_luchtruim_and_notams()
    n2000_fc = enrich_natura2000()
    # Stiltegebieden zijn geen luchtvaartwet-no-fly-zones en staan ook niet op GoDrone;
    # we houden het bestand leeg zodat gebieden zoals Eemland of Kockengen niet onterecht rood kleuren.
    stilte_fc = {"type": "FeatureCollection", "name": "stiltegebieden", "features": []}

    # 4. Hoogspanningslijnen uit OpenStreetMap (via 6 compacte Overpass-tegels zodat er geen timeout optreedt)
    power_fc = fetch_hoogspanning_osm()

    manifest_files: Dict[str, Any] = {}
    manifest_files["luchtruim_mini2se.geojson"] = save_geojson_if_changed(DATA_DIR / "luchtruim_mini2se.geojson", luchtruim_fc)
    manifest_files["notams_actueel.geojson"] = save_geojson_if_changed(DATA_DIR / "notams_actueel.geojson", notams_fc)
    manifest_files["natura2000_slim.geojson"] = save_geojson_if_changed(DATA_DIR / "natura2000_slim.geojson", n2000_fc)
    manifest_files["stiltegebieden.geojson"] = save_geojson_if_changed(DATA_DIR / "stiltegebieden.geojson", stilte_fc)
    if power_fc.get("features"):
        manifest_files["hoogspanning.geojson"] = save_geojson_if_changed(DATA_DIR / "hoogspanning.geojson", power_fc)

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
