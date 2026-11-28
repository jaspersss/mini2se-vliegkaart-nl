#!/usr/bin/env python3
"""
update_geodata.py — Officiële GoDrone / LVNL ED-318 UAS Geozones & PDOK Natura 2000 Pipeline
============================================================================================
Dit script haalt de exacte, actuele Nederlandse drone-zonering op (inclusief de nieuwe
gelaagde CTR-zones rond civiele luchthavens, laagvliegcorridors en live NOTAMs/TGB's):

1. `data/luchtruim_mini2se.geojson`:
   Haalt via de officiële GoDrone / LVNL MapData API (`api.godrone.nl/v2/mapdata/geojson`)
   alle Nederlandse **ED-318 UAS Geozones** (`nl:uas:zones:ed318:*`), laagvliegcorridors
   en waarschuwingszones voor verhoogde luchtactiviteit op:
   - **Nieuwe gelaagde Civiele CTR-zones** (Schiphol, Rotterdam, Groningen, Lelystad, Maastricht):
     * **CTR ZONE 1 (Kernzone, 0-120m AGL):** 🔴 `forbidden` voor Open Categorie (0m).
     * **CTR ZONE 2A (Tussenring onderlaag, 0-45m AGL) & 2B (45-120m AGL):** 🔴 `forbidden` voor Open Categorie (alleen Specific tot 45m).
     * **CTR ZONE 3 (Buitenste ring, 0-120m AGL):** 🟢 **TOEGESTAAN in de OPEN categorie (tot 120m AGL)**.
   - **Militaire CTR's** (Eindhoven, Gilze-Rijen, Volkel, Woensdrecht, Leeuwarden, De Kooy, Deelen, De Peel): 🔴 `forbidden` (0m).
   - **Laagvlieggebieden & -corridors (ED-318 groep `2.*` & VFR-laagvliegroutes):** 🟠 `caution` — **OPEN A1/A2 toegestaan tot max. 30m hoogte boven de grond; A3 verboden.**
   - **Beveiligde gebieden, gevangenissen, paleizen, vitale infrastructuur, zware industrie & havens (ED-318 groepen `3.*`, `4.*`, `5.*`, `6.*`, `7.*`):** 🔴 `forbidden` (0m voor Open Categorie).
   - **Gebieden met verhoogd luchtverkeersrisico (`risk:increasedAerialActivity:*`):** ℹ️ `info` (max 120m, extra waakzaamheid voor zweefvliegers, parachutisten of modelvliegers).

2. `data/notams_actueel.geojson`:
   Haalt alle actuele **live Nederlandse NOTAMs & TFR's (Tijdelijke Gebieden met Beperkingen)**
   onder 120m AGL rechtstreeks uit de GoDrone/LVNL feed en markeert ze veilig als no-go (`forbidden`, 0m).

3. `data/natura2000_slim.geojson` & `data/stiltegebieden.geojson`:
   - Alle 209 Natura 2000-gebieden op 100% originele PDOK-geometrie, gekoppeld aan `data/n2000_overrides.json`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
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

# De sleutel wordt NIET meer in de repository opgeslagen; zet hem als GitHub Actions Secret
# `GODRONE_API_KEY` (lokaal: `export GODRONE_API_KEY=...`). Zonder sleutel wordt de vorige
# complete GoDrone-dataset behouden en krijgt de bron status "cached" in meta.json.
GODRONE_API_KEY = os.environ.get("GODRONE_API_KEY", "").strip()
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
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
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


def fetch_godrone_luchtruim_and_notams() -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """
    Haalt alle officiële Nederlandse ED-318 UAS Geozones (inclusief de nieuwe gelaagde
    civiele CTR-zones 1, 2A en 3), laagvliegcorridors, risicogebieden en live Nederlandse NOTAMs
    rechtstreeks op uit de GoDrone / LVNL MapData API.
    Retourneert (luchtruim_fc, notams_fc, all_tiles_ok).
    """
    if not GODRONE_API_KEY:
        logger.error("GODRONE_API_KEY ontbreekt (GitHub Secret niet ingesteld); behoud vorige GoDrone-datasets.")
        return (
            load_local_json(DATA_DIR / "luchtruim_mini2se.geojson"),
            load_local_json(DATA_DIR / "notams_actueel.geojson"),
            False,
        )

    headers = {
        "Authorization": f"X-AA-ApiKey {GODRONE_API_KEY}",
        "Origin": "https://map.godrone.nl",
        "Referer": "https://map.godrone.nl/",
        "Accept-Language": "nl-NL,nl;q=0.9",
    }

    ed318_features: Dict[str, Dict[str, Any]] = {}
    risk_features: Dict[str, Dict[str, Any]] = {}
    notam_features: Dict[str, Dict[str, Any]] = {}
    failed_tiles = 0

    for s, n, w, e in GODRONE_TILES:
        url = f"https://api.godrone.nl/v2/mapdata/geojson?n={n}&s={s}&e={e}&w={w}&is_Compact=false"
        tile_data = fetch_json_url(url, headers=headers, timeout=30)
        if tile_data is None:
            failed_tiles += 1
            logger.warning("GoDrone-tegel (%s,%s,%s,%s) kon niet worden opgehaald.", s, n, w, e)
            continue
        if not tile_data.get("features"):
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

            # 2. Live NOTAMs & TFR's (uitsluitend Nederlands luchtruim EHAA / nl:)
            elif "notam" in aid.lower() or dcat in ("class:tfr", "class:nav_warning"):
                if not (aid.lower().startswith("nl:") or "ehaa" in aid.lower() or "ehaa" in name.lower()):
                    continue
                # Filter pure obstakel-NOTAMs (mastverlichting, kranen, windmolens)
                if "OBSTACLE" in name.upper() or "WINDMILL" in name.upper() or "CRANE" in name.upper():
                    continue
                notam_id = str(p.get("notamId") or aid)
                notam_features[notam_id] = feat

            # 3. Civiele / overige Laagvliegroutes & -gebieden (o.a. VFR-corridors bij Schiphol/Loenersloot)
            elif "low_fly" in dcat.lower() or "laagvlieg" in name.lower():
                ed318_features[f"LFR_{aid}"] = feat

            # 4. Waarschuwingsgebieden (Zweefvliegen, Modelvliegen/UAV, Paragliding):
            # Puur subtiel als stippelcirkel + VLOS-melding, zonder de bovenbalk oranje te maken.
            elif dcat.startswith("risk:increasedAerialActivity:"):
                key = f"{dcat}:{name}"
                risk_features[key] = feat

    luchtruim_out: List[Dict[str, Any]] = []

    # Verwerk ED-318 UAS Geozones & Laagvliegcorridors
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
                        "Vluchten in de OPEN categorie (A1, A2 en A3) zijn hier zonder luchtvaart-restricties TOEGESTAAN tot 120m hoogte."
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
                        f"Nieuwe gelaagde CTR-zonering ({name}): Categorie OPEN (A1/A2/A3) is hier VERBODEN (0m). "
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
                        f"Binnenste kernzone van de luchthaven ({name}, 0-120m): Categorie OPEN (A1/A2/A3) is hier strikt VERBODEN (0m). "
                        "Alleen toegankelijk voor Specifieke categorie onder artikel 3a lid 3 met GoDrone missieplan en RT-contact."
                    ),
                },
                "geometry": preserve_full_geometry(feat["geometry"], precision=5),
            })

        # B. Laagvlieggebieden & Laagvliegroutes (ED-318 groep 2.* én overige laagvliegcorridors)
        elif aid.startswith("nl:uas:zones:ed318:2.") or aid.startswith("LFR_"):
            luchtruim_out.append({
                "type": "Feature",
                "properties": {
                    "id": aid,
                    "naam": name,
                    "type": "GLV_LOW_FLY",
                    "categorie": "Laagvliegroute / -gebied Bemand Luchtverkeer",
                    "status_active": "caution",
                    "status_inactive": "free",
                    "max_height_active_m": 30,
                    "max_height_inactive_m": 120,
                    "forbidden_for_a3": True,
                    "schedule": "H24",
                    "bron": "GoDrone / LVNL geozone (Ministerie van IenW)",
                    "mini2se_toelichting": (
                        f"Laagvliegroute / -gebied ({name}): In Open Categorie A1 en A2 toegestaan tot maximaal 30 meter boven de grond; "
                        "in Open Categorie A3 is vliegen hier VERBODEN. Let altijd op plotseling laagvliegende helikopters of vliegtuigen."
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

    # Verwerk Subtiele Waarschuwingszones (Zweefvliegen, Modelvliegen/UAV, Paragliding)
    for key, feat in sorted(risk_features.items()):
        p = feat.get("properties") or {}
        name = str(p.get("name") or "Luchtactiviteit").strip()
        disp = p.get("display") or {}
        activity = "Luchtactiviteit"
        for sec in disp.get("sections") or []:
            if sec.get("displayTitle") == "Activiteit" or sec.get("title") == "Activity":
                activity = strip_html(str(sec.get("text") or "Luchtvaart"))
                break

        act_nl = {
            "GLIDING": "Zweefvliegen",
            "PARAGLIDER": "Paragliding",
            "UAV": "Modelvliegen / Drones",
        }.get(activity.upper())

        if not act_nl:
            continue

        luchtruim_out.append({
            "type": "Feature",
            "properties": {
                "id": f"RISK_{key}",
                "naam": f"{act_nl} ({name})",
                "type": "WARNING_CIRCLE",
                "categorie": f"Waarschuwingsgebied ({act_nl})",
                "status": "info",
                "max_height_m": 120,
                "schedule": "H24",
                "mini2se_toelichting": (
                    f"Waarschuwingsgebied op GoDrone ({act_nl} — {name}). "
                    "Geen wettelijk vliegverbod voor jouw drone (vrij tot 120m AGL), "
                    "maar houd het luchtruim in de gaten en geef voorrang aan zweefvliegers/luchtverkeer."
                ),
            },
            "geometry": preserve_full_geometry(feat["geometry"], precision=5),
        })

    # Verwerk Live Nederlandse NOTAMs & TFR's (veiligheidshalve allemaal als no-go / 0m)
    notams_out: List[Dict[str, Any]] = []
    for notam_id, feat in sorted(notam_features.items()):
        p = feat.get("properties") or {}
        name = str(p.get("name") or notam_id).strip()
        dcat = str(p.get("detailedCategory") or "")
        is_tfr = dcat == "class:tfr"
        op_from = p.get("operationalFrom") or "2025-01-01T00:00:00Z"
        op_to = p.get("operationalTo") or "2099-12-31T23:59:59Z"

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
                "status": "forbidden",
                "max_height_m": 0,
                "valid_from": op_from,
                "valid_to": op_to,
                "mini2se_toelichting": (
                    f"{'Tijdelijk Gebied met Beperkingen (TFR / No-Fly NOTAM)' if is_tfr else 'Tijdelijke Navigatiewaarschuwing (NOTAM)'} "
                    f"[{notam_id}]: {full_text or name} — Uit veiligheidsoogpunt gemarkeerd als no-go (0m)."
                ),
            },
            "geometry": preserve_full_geometry(feat["geometry"], precision=5),
        })

    if failed_tiles > 0 or not luchtruim_out:
        logger.warning(
            "GoDrone update onvolledig (%d/%d tegels mislukt); behoud bestaande luchtruim_mini2se.geojson en notams_actueel.geojson.",
            failed_tiles,
            len(GODRONE_TILES),
        )
        return (
            load_local_json(DATA_DIR / "luchtruim_mini2se.geojson"),
            load_local_json(DATA_DIR / "notams_actueel.geojson"),
            False,
        )

    logger.info("GoDrone ED-318 geozones + corridors + risicogebieden opgehaald: %d luchtruimzones, %d live NL NOTAMs", len(luchtruim_out), len(notams_out))
    return (
        {"type": "FeatureCollection", "name": "luchtruim_mini2se", "features": luchtruim_out},
        {"type": "FeatureCollection", "name": "notams_actueel", "features": notams_out},
        True,
    )


def fetch_natura2000_url_map() -> Dict[str, str]:
    """Haalt alle directe gebieds-URL's op van natura2000.nl/gebieden."""
    url_map: Dict[str, str] = {}
    try:
        req = urllib.request.Request(
            "https://www.natura2000.nl/gebieden",
            headers={"User-Agent": "Mini2SE-Vliegkaart-NL/2.0"},
        )
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        for href, text in re.findall(r'href="(/gebieden/[^/]+/[^"]+)"[^>]*>([^<]+)<', html):
            clean_name = re.sub(r"\s+", " ", text).strip().lower()
            if clean_name:
                url_map[clean_name] = f"https://www.natura2000.nl{href}"
    except Exception as exc:
        logger.warning("Kon natura2000.nl/gebieden links niet ophalen: %s", exc)
    return url_map


def enrich_natura2000() -> Tuple[Dict[str, Any], bool]:
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
    fresh = bool(raw_n2000 and raw_n2000.get("features"))
    if not fresh:
        logger.warning("PDOK Natura 2000 WFS niet bereikbaar; behoud bestaande natura2000_slim.geojson.")
        return load_local_json(target_path), False

    n2k_url_map = fetch_natura2000_url_map()

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

        bescherming = str(props.get("beschermin") or "").strip()
        besluit_txt = str(props.get("status") or "").strip()
        staatscour = str(props.get("staatscour") or "").strip()
        bron_info = f" [{besluit_txt}, Stcrt. {staatscour}]" if ("besluit" in besluit_txt.lower() and staatscour) else ""

        water_keywords = (
            "zee", "kustzone", "voordelta", "delta", "estuarium", "schel", "grevelingen",
            "krammer", "volkerak", "haringvliet", "hollandsch diep", "ketelmeer", "zwarte meer",
            "veerse meer", "eems", "dollard", "friese front", "doggersbank", "klaverbank", "bruine bank",
            "zoommeer", "markiezaat", "ijmeer", "markermeer", "ijsselmeer", "randmeren", "waddenzee"
        )
        is_water_area = any(k in str(naam).lower() for k in water_keywords)

        if override.get("mini2se_toelichting"):
            toelichting = f"{override['mini2se_toelichting']}{bron_info}"
        elif is_water_area:
            toelichting = (
                f"<strong>Vliegen:</strong> Ja, toegestaan boven open water (geen algemeen droneverbod in het Natura 2000-besluit van {naam}).<br>"
                "<strong>Opstijgen:</strong> Toegestaan vanaf openbare stranden, dijken en kades (of vanaf een boot); niet vanaf afgesloten vogelbroedstranden.<br>"
                f"<strong>Afstand houden:</strong> Min. 100–150m van rustende watervogels, broedkolonies en zeehonden.{bron_info}"
            )
        elif "VR" in bescherming:
            toelichting = (
                f"<strong>Vliegen:</strong> Ja, toegestaan (geen vliegverbod in het Natura 2000-besluit van {naam}).<br>"
                "<strong>Opstijgen:</strong> Alleen vanaf openbare grond/wegen; verboden vanaf terrein van Staatsbosbeheer of Natuurmonumenten.<br>"
                f"<strong>Afstand houden:</strong> Min. 100m van vogels (extra streng in broedseizoen 15 mrt–15 jul).{bron_info}"
            )
        elif bescherming == "HR":
            toelichting = (
                f"<strong>Vliegen:</strong> Ja, toegestaan (geen droneverbod in het Natura 2000-besluit van {naam}).<br>"
                "<strong>Opstijgen:</strong> Alleen vanaf openbare grond/wegen; niet opstijgen vanaf terrein van Staatsbosbeheer of Natuurmonumenten.<br>"
                f"<strong>Afstand houden:</strong> Houd afstand van wild en verstoor geen dieren.{bron_info}"
            )
        else:
            toelichting = f"{default_advice}{bron_info}"

        max_height = 0 if status == "forbidden" else int(override.get("max_height_m", 120))
        direct_url = n2k_url_map.get(str(naam).strip().lower())
        url = direct_url or override.get("url") or "https://www.natura2000.nl/gebieden"
        default_beheerder = "Rijkswaterstaat" if is_water_area else "Staatsbosbeheer / Natuurmonumenten / Provincie"
        beheerder = override.get("beheerder") or props.get("beheerder") or default_beheerder

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

    return {"type": "FeatureCollection", "name": "natura2000_slim", "features": enriched_features}, True


def fetch_nationale_parken_pdok() -> List[Dict[str, Any]]:
    """Haalt de exacte grenzen van de Nederlandse Nationale Parken op uit PDOK CDDA (o.a. De Hoge Veluwe & Veluwezoom)."""
    url = "https://api.pdok.nl/rvo/nationaal-beschermde-gebieden-cdda/ogc/v1/collections/cdda/items?f=json&limit=250"
    raw = fetch_json_url(url, timeout=30)
    if not raw or not raw.get("features"):
        return []

    np_names = {
        "de hoge veluwe", "veluwezoom", "utrechtse heuvelrug", "zuid-kennemerland",
        "de biesbosch", "sallandse heuvelrug", "dwingelderveld", "de groote peel",
        "de maasduinen", "de meinweg", "de alde feanen", "lauwersmeer",
        "schiermonnikoog", "duinen van texel", "weerribben-wieden", "drents-friese wold",
        "oosterschelde", "zoommeer", "loonse en drunense duinen", "nieuw land", "van het huis"
    }

    strict_forbidden_nps = {
        "de hoge veluwe", "veluwezoom", "de biesbosch", "zuid-kennemerland", "nieuw land"
    }

    out: List[Dict[str, Any]] = []
    seen = set()
    for feat in raw["features"]:
        geom = feat.get("geometry")
        if not geom:
            continue
        p = feat.get("properties") or {}
        name = str(p.get("sitename") or "").strip()
        desval = str(p.get("desvalue") or "").strip()
        if name.lower() in np_names or "nationaal park" in desval.lower():
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            is_strict = name.lower() in strict_forbidden_nps
            out.append({
                "type": "Feature",
                "properties": {
                    "id": f"NP_{p.get('localid') or name}",
                    "naam": f"Nationaal Park {name}",
                    "type": "NATIONAL_PARK",
                    "categorie": "Nationaal Park (Terreinregels)",
                    "status": "forbidden" if is_strict else "caution",
                    "max_height_m": 0 if is_strict else 120,
                    "schedule": "H24",
                    "mini2se_toelichting": (
                        f"Nationaal Park {name}: via de toegangsvoorwaarden en terreinregels van dit Nationaal Park "
                        "geldt een strikt opstijg- en vliegverbod voor drones (ook < 250g). Buiten de parkgrens geldt dit parkverbod niet."
                        if is_strict
                        else f"Nationaal Park {name}: geen algemeen parkbreed vliegverbod (je mag vanaf openbare grond/wegen vliegen tot 120m), "
                             "maar let op de terreinregels van specifieke eigenaren (zoals Staatsbosbeheer, Natuurmonumenten of Utrechts Landschap) en verstoor geen fauna."
                    ),
                },
                "geometry": preserve_full_geometry(geom, precision=5),
            })

    logger.info("Officiële Nationale Parken opgehaald uit PDOK CDDA: %d parken", len(out))
    return out


def fetch_hoogspanning_osm() -> Dict[str, Any]:
    """Haalt Nederlandse hoogspanningslijnen (power=line) op uit OpenStreetMap via 6 kleine tegels."""
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

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path = DATA_DIR / "meta.json"
    prev_meta = load_local_json(meta_path) if meta_path.exists() else {}
    prev_sources = prev_meta.get("sources") or {}

    luchtruim_fc, notams_fc, godrone_ok = fetch_godrone_luchtruim_and_notams()
    n2000_fc, n2000_ok = enrich_natura2000()
    stilte_fc = {"type": "FeatureCollection", "name": "stiltegebieden", "features": []}

    # Nationale Parken zijn tijdelijk gepauzeerd totdat de grondeigenaren-laag is geïmplementeerd.
    warnings: List[str] = []
    if not godrone_ok:
        warnings.append("GoDrone (luchtruim + NOTAMs) niet volledig opgehaald; vorige complete dataset behouden.")
    if not n2000_ok:
        warnings.append("PDOK Natura 2000 niet bereikbaar; vorige dataset behouden.")

    # Hoogspanningslijnen (statisch bestand in /data/hoogspanning.geojson, geen Overpass-calls in CI)
    power_path = DATA_DIR / "hoogspanning.geojson"
    power_fc = load_local_json(power_path)

    manifest_files: Dict[str, Any] = {}
    manifest_files["luchtruim_mini2se.geojson"] = save_geojson_if_changed(DATA_DIR / "luchtruim_mini2se.geojson", luchtruim_fc)
    manifest_files["notams_actueel.geojson"] = save_geojson_if_changed(DATA_DIR / "notams_actueel.geojson", notams_fc)
    manifest_files["natura2000_slim.geojson"] = save_geojson_if_changed(DATA_DIR / "natura2000_slim.geojson", n2000_fc)
    manifest_files["stiltegebieden.geojson"] = save_geojson_if_changed(DATA_DIR / "stiltegebieden.geojson", stilte_fc)
    if power_fc.get("features"):
        manifest_files["hoogspanning.geojson"] = save_geojson_if_changed(DATA_DIR / "hoogspanning.geojson", power_fc)

    def source_entry(key: str, ok: bool, fc: Dict[str, Any]) -> Dict[str, Any]:
        prev = prev_sources.get(key) or {}
        has_data = bool(fc.get("features"))
        return {
            "status": "ok" if ok else ("cached" if has_data else "error"),
            "last_attempt": now_iso,
            "last_success": now_iso if ok else prev.get("last_success"),
            "count": len(fc.get("features") or []),
        }

    sources_meta = {
        "godrone_luchtruim": source_entry("godrone_luchtruim", godrone_ok, luchtruim_fc),
        "godrone_notams": source_entry("godrone_notams", godrone_ok, notams_fc),
        "natura2000": source_entry("natura2000", n2000_ok, n2000_fc),
    }
    for w in warnings:
        print(f"::warning::{w}")

    meta_doc = {
        "updated_at": now_iso,
        "sources": sources_meta,
        "warnings": warnings,
        "files": manifest_files,
    }
    meta_bytes = json.dumps(meta_doc, indent=2, sort_keys=True).encode("utf-8")
    if not meta_path.exists() or meta_path.read_bytes() != meta_bytes:
        meta_path.write_bytes(meta_bytes)
        logger.info("Versie-manifest data/meta.json bijgewerkt.")
    else:
        logger.info("Versie-manifest data/meta.json is ongewijzigd.")


if __name__ == "__main__":
    main()
