#!/usr/bin/env python3
"""
update_incidents.py — Actieve hulpverleningsincidenten (P2000) voor de dronekaart
=================================================================================
Leest de publieke Alarmeringen.nl RSS-feed (geen coördinaten, ~38 minuten historie voor
heel Nederland), classificeert relevante meldingen, geocodeert ze via PDOK Locatieserver
(gratis, geen sleutel) en houdt per incident een vervaltijd (TTL) bij.

Privacy: er wordt GEEN ruwe P2000-tekst, straatnaam of huisnummer opgeslagen of gepubliceerd.
 - state.json bevat alleen categorie, afgeronde coördinaten, tijden en gehashte meldings-ID's.
 - incidents.json (publiek, voor de app) bevat alleen categorie, middelpunt (~100m afgerond),
   straal en vervaltijd.

Gebruik: python scripts/update_incidents.py --dir <map met state.json/incidents.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("update_incidents")

FEED_URL = "https://alarmeringen.nl/feeds/all.rss"
PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
GROUP_RADIUS_M = 1500

RE_MMT = re.compile(r"\b(lifeliner|mmt|traumaheli)\b", re.I)
RE_GRIP_START = re.compile(r"\bgrip\s*[1-4]\b", re.I)
RE_GRIP_END = re.compile(r"\b(einde\s*grip|grip\s*0|afschaling\s*grip)\b", re.I)
RE_NATUURBRAND = re.compile(
    r"\b(natuurbrand|bosbrand|heidebrand|duinbrand|brand\s+(natuur|bos|heide|duin|riet))\b", re.I
)
# Spec: middel|grote|zeer grote|grip — "middel\w*" vangt ook "middelbrand"
RE_BIG_FIRE = re.compile(r"\b(middel\w*|grote|zeer\s*grote|grip)\b", re.I)
RE_BRANDMEESTER = re.compile(r"\bbrand\s*meester\b", re.I)
RE_DRONE = re.compile(
    r"\b(inzet\s*drone|droneteam|drone\s*team|team\s*digitale\s*verkenning|tdv|uas)\b", re.I
)

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "mmt": {"label": "Traumaheli (MMT)", "radius": 2000},
    "grip": {"label": "GRIP-incident", "radius": 2000},
    "natuurbrand": {"label": "Natuurbrand", "radius": 2000},
    "drone": {"label": "Inzet drone hulpdiensten", "radius": 2000},
}
BIG_FIRE_RADIUS = 4500

# Plaats-/meldkamer-fallbackcoördinaten die we nooit als incidentlocatie accepteren
BLOCKED_WORDS = ("meldkamer", "brandweerkazerne", "ambulancepost")


# ----------------------------------------------------------------------------------------------
# Hulpfuncties
# ----------------------------------------------------------------------------------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sha(s: str, n: int = 16) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def fetch_url(url: str, timeout: int = 20) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "Dronekaart-Jasper/1.0 (+https://drone.jasperneeleman.nl)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception as exc:  # noqa: BLE001 — netwerk/parse fouten mogen de pipeline niet laten crashen
        logger.warning("Ophalen mislukt (%s): %s", url.split("?")[0], exc)
    return None


def parse_feed(xml_bytes: bytes) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        guid = (it.findtext("guid") or it.findtext("link") or title).strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        items.append({"guid": guid, "title": title, "desc": desc, "time": pub})
    items.sort(key=lambda x: x["time"])
    return items


def classify(text: str) -> List[str]:
    """Geeft alle categorieën waarvoor de melding een trigger bevat."""
    cats = []
    if RE_MMT.search(text):
        cats.append("mmt")
    if RE_GRIP_START.search(text) and not RE_GRIP_END.search(text):
        cats.append("grip")
    if RE_NATUURBRAND.search(text):
        cats.append("natuurbrand")
    if RE_DRONE.search(text):
        cats.append("drone")
    return cats


def extract_street_place(desc: str) -> Optional[Tuple[str, str]]:
    """'Politie naar Provincialeweg in Ingen voor ongeval' -> ('Provincialeweg', 'Ingen')."""
    d = re.split(r"\s+voor\s+", desc, maxsplit=1)[0].strip()
    if " in " not in d:
        return None
    left, place = d.rsplit(" in ", 1)
    m = re.match(r"^.*?\b(?:naar|op|bij|nabij|langs|aan)\s+(.+)$", left, re.I)
    if not m:
        return None
    street = m.group(1).strip()
    place = place.strip()
    if not street or not place or street.lower() == place.lower():
        return None
    if any(w in street.lower() for w in BLOCKED_WORDS):
        return None
    return street, place


def pdok_geocode(street: str, place: str) -> Optional[Tuple[float, float]]:
    """
    Geocodeert een straat in een plaats. Accepteert alleen resultaten van type 'weg' in
    precies die woonplaats: een generiek plaatsmiddelpunt (fallback) wordt NOOIT gebruikt.
    """
    params = urllib.parse.urlencode([("q", f"{street} {place}"), ("rows", 10), ("fq", "type:weg"),
                                     ("fq", f'woonplaatsnaam:"{place}"'), ("fl", "weergavenaam,straatnaam,woonplaatsnaam,centroide_ll,type")])
    raw = fetch_url(f"{PDOK_URL}?{params}")
    if not raw:
        return None
    try:
        docs = json.loads(raw.decode("utf-8")).get("response", {}).get("docs", [])
    except ValueError:
        return None
    for doc in docs:
        if doc.get("type") != "weg":
            continue
        if str(doc.get("woonplaatsnaam", "")).lower() != place.lower():
            continue
        # Fuzzy-treffers (PDOK geeft ook bij onbekende straten "iets in de buurt") weigeren:
        # de straatnaam moet exact overeenkomen.
        if str(doc.get("straatnaam", "")).strip().lower() != street.strip().lower():
            continue
        m = re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", str(doc.get("centroide_ll", "")))
        if m:
            return float(m.group(2)), float(m.group(1))
    return None


# ----------------------------------------------------------------------------------------------
# Kernlogica
# ----------------------------------------------------------------------------------------------
def find_incident(state: Dict[str, Any], cat: str, lat: float, lon: float, now: datetime) -> Optional[Dict[str, Any]]:
    for inc in state["incidents"]:
        if inc["cat"] != cat or parse_iso(inc["expires"]) <= now:
            continue
        if haversine_m(inc["lat"], inc["lon"], lat, lon) <= GROUP_RADIUS_M:
            return inc
    return None


def upsert(state: Dict[str, Any], cat: str, lat: float, lon: float, msg_time: datetime, now: datetime) -> Dict[str, Any]:
    inc = find_incident(state, cat, lat, lon, now)
    if inc is None:
        inc = {
            "id": sha(f"{cat}{lat:.4f}{lon:.4f}{iso(msg_time)}"),
            "cat": cat,
            "lat": round(lat, 3),  # ~100m: ook de publieke state bevat geen exacte adrescoördinaten
            "lon": round(lon, 3),
            "radius": CATEGORIES[cat]["radius"],
            "first": iso(msg_time),
            "last": iso(msg_time),
            "expires": iso(msg_time),
            "grip_end": None,
        }
        state["incidents"].append(inc)
    inc["last"] = iso(max(msg_time, parse_iso(inc["last"])))
    return inc


def apply_ttl(inc: Dict[str, Any], msg_time: datetime) -> None:
    cat = inc["cat"]
    cur = parse_iso(inc["expires"])
    if cat == "mmt":
        new = msg_time + timedelta(minutes=90)
    elif cat == "grip":
        if inc.get("grip_end"):
            new = parse_iso(inc["grip_end"]) + timedelta(minutes=15)
        else:
            new = msg_time + timedelta(hours=6)  # harde fallback-TTL
    elif cat == "natuurbrand":
        new = msg_time + timedelta(hours=3)  # sliding TTL
    else:  # drone
        new = msg_time + timedelta(minutes=150)
    # natuurbrand: sliding TTL (reset bij elk nieuw bericht); overige categorieën verlengen alleen
    inc["expires"] = iso(new if cat == "natuurbrand" else max(cur, new))


def process_items(
    items: List[Dict[str, Any]],
    state: Dict[str, Any],
    now: datetime,
    geocode: Callable[[str, str], Optional[Tuple[float, float]]] = pdok_geocode,
) -> None:
    geo_cache: Dict[str, Optional[List[float]]] = state.setdefault("geo_cache", {})

    def locate(desc: str) -> Optional[Tuple[float, float]]:
        sp = extract_street_place(desc)
        if not sp:
            return None
        key = sha(f"{sp[0].lower()}|{sp[1].lower()}")  # gehasht: geen straatnamen in de publieke state
        if key not in geo_cache:
            res = geocode(*sp)
            geo_cache[key] = [round(res[0], 3), round(res[1], 3)] if res else None
        v = geo_cache[key]
        return (v[0], v[1]) if v else None

    seen = state.setdefault("seen", {})
    for it in items:
        gid = sha(it["guid"])
        if gid in seen:
            continue
        seen[gid] = iso(it["time"])
        text = f"{it['title']} {it['desc']}"
        t = it["time"]
        cats = classify(text)

        has_active = lambda c: any(i["cat"] == c and parse_iso(i["expires"]) > now for i in state["incidents"])  # noqa: E731
        end_grip = bool(RE_GRIP_END.search(text)) and has_active("grip")
        linked_fire = (
            has_active("natuurbrand")
            and not cats
            and re.search(r"brand", text, re.I)
            and (RE_BIG_FIRE.search(text) or RE_BRANDMEESTER.search(text))
        )
        if not (cats or end_grip or linked_fire):
            continue

        pos = locate(it["desc"])
        if pos is None:
            logger.info("Relevante melding overgeslagen: geen betrouwbare locatie (geen vals plaatsmiddelpunt).")
            continue
        lat, lon = pos

        if end_grip:
            inc = find_incident(state, "grip", lat, lon, now)
            if inc:
                inc["grip_end"] = iso(t)
                inc["expires"] = iso(t + timedelta(minutes=15))
        if linked_fire:
            inc = find_incident(state, "natuurbrand", lat, lon, now)
            if inc:
                if RE_BIG_FIRE.search(text):
                    inc["radius"] = BIG_FIRE_RADIUS
                if RE_BRANDMEESTER.search(text):
                    inc["expires"] = iso(t + timedelta(minutes=90))
                else:
                    apply_ttl(inc, t)
                inc["last"] = iso(max(t, parse_iso(inc["last"])))

        for cat in cats:
            inc = upsert(state, cat, lat, lon, t, now)
            if cat == "natuurbrand":
                if RE_BIG_FIRE.search(text):
                    inc["radius"] = BIG_FIRE_RADIUS
                if RE_BRANDMEESTER.search(text):
                    inc["expires"] = iso(t + timedelta(minutes=90))
                else:
                    apply_ttl(inc, t)
            else:
                if cat == "grip":
                    inc["grip_end"] = None  # nieuwe GRIP-start heropent een eerder afgeschaald incident
                apply_ttl(inc, t)


def finalize(state: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    # Drone-inzet loopt mee met een gekoppeld incident als dat langer duurt
    for d in state["incidents"]:
        if d["cat"] != "drone":
            continue
        for o in state["incidents"]:
            if o is d or o["cat"] == "drone":
                continue
            if haversine_m(d["lat"], d["lon"], o["lat"], o["lon"]) <= GROUP_RADIUS_M:
                if parse_iso(o["expires"]) > parse_iso(d["expires"]):
                    d["expires"] = o["expires"]
    state["incidents"] = [i for i in state["incidents"] if parse_iso(i["expires"]) > now]
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if parse_iso(v) > now - timedelta(hours=3)}
    out = []
    for i in state["incidents"]:
        out.append({
            "id": i["id"],
            "category": i["cat"],
            "label": CATEGORIES[i["cat"]]["label"],
            # ~100m afgerond: de exacte locatie van het adres wordt niet gepubliceerd
            "lat": round(i["lat"], 3),
            "lon": round(i["lon"], 3),
            "radius_m": i["radius"],
            "updated_at": i["last"],
            "expires_at": i["expires"],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path, out_path = out_dir / "state.json", out_dir / "incidents.json"

    now = datetime.now(timezone.utc)
    state: Dict[str, Any] = {"incidents": [], "seen": {}, "geo_cache": {}}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("state.json onleesbaar; start met lege state.")

    raw = fetch_url(FEED_URL)
    feed_ok = False
    if raw:
        try:
            process_items(parse_feed(raw), state, now)
            feed_ok = True
        except ET.ParseError as exc:
            logger.warning("RSS-feed onleesbaar: %s", exc)
    if not feed_ok:
        print("::warning::Alarmeringen.nl RSS niet bereikbaar; bestaande incidenten blijven staan tot hun TTL.")

    incidents = finalize(state, now)
    state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    last_ok = iso(now) if feed_ok else None
    if not feed_ok and out_path.exists():
        try:
            last_ok = json.loads(out_path.read_text(encoding="utf-8")).get("last_feed_success")
        except ValueError:
            pass
    out_path.write_text(json.dumps({
        "generated_at": iso(now),
        "feed_ok": feed_ok,
        "last_feed_success": last_ok,
        "incidents": incidents,
    }, indent=1), encoding="utf-8")
    logger.info("Klaar: %d actieve incidenten (feed_ok=%s).", len(incidents), feed_ok)


if __name__ == "__main__":
    main()
