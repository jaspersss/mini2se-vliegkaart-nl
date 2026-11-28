#!/usr/bin/env python3
"""
update_incidents.py — privacybewuste P2000-incidentfeed voor de dronekaart.

Vervangt de bestaande updater. Compatibel met de bestaande CLI:
    python scripts/update_incidents.py --dir <directory>

Input:
  <directory>/state.json       interne, persistente verwerkingsstatus
  <directory>/incidents.json  publieke feed voor de frontend

Belangrijke ontwerpkeuzes:
- Een melding wordt pas als verwerkt gemarkeerd nadat de verwerking klaar is.
- Tijdelijke geocoderingsfouten krijgen een retry; negatieve geocoderesultaten
  worden slechts kort gecachet.
- Incidenten worden per categorie met categorie-specifieke afstands- en
  tijdvensters gekoppeld. Dit blijft een heuristiek: de RSS-bron levert geen
  betrouwbare, gedeelde incident-ID waarmee verschillende meldingen met
  zekerheid aan hetzelfde incident kunnen worden gekoppeld.
- Drone-inzetten worden NIET automatisch aan elk nabijgelegen incident gekoppeld.
  Zonder expliciete bronrelatie zou dat onterechte verlenging veroorzaken.
- Bij een onleesbare state stopt het script in plaats van stilzwijgend een lege
  state te publiceren.
- Publieke output bevat geen ruwe P2000-tekst, straatnamen of huisnummers.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("update_incidents")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

FEED_URL = "https://alarmeringen.nl/feeds/all.rss"
PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
USER_AGENT = "Dronekaart-Jasper/2.0 (+https://drone.jasperneeleman.nl)"
SCHEMA_VERSION = 2

# Categorie-specifieke matching. Dit zijn technische deduplicatievensters,
# geen veiligheidsafstanden en geen officiële incidentgrenzen.
MATCHING: Dict[str, Dict[str, int]] = {
    "mmt": {"radius_m": 300, "window_minutes": 45},
    "grip": {"radius_m": 1500, "window_minutes": 360},
    "natuurbrand": {"radius_m": 1500, "window_minutes": 360},
    "drone": {"radius_m": 400, "window_minutes": 90},
}

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "mmt": {"label": "Traumaheli (MMT)", "radius": 2000, "ttl_minutes": 90},
    "grip": {"label": "GRIP-incident", "radius": 2000, "ttl_minutes": 360},
    "natuurbrand": {"label": "Natuurbrand", "radius": 2000, "ttl_minutes": 180},
    "drone": {"label": "Inzet drone hulpdiensten", "radius": 2000, "ttl_minutes": 150},
}
BIG_FIRE_RADIUS_M = 4500
BRANDMEESTER_TTL_MINUTES = 90

RE_MMT = re.compile(r"\b(lifeliner|ll\s*[1-4]|mmt|traumaheli)\b", re.I)
RE_GRIP_START = re.compile(r"\bgrip\s*[1-4]\b", re.I)
RE_GRIP_END = re.compile(r"\b(einde\s*grip|grip\s*0|afschaling\s*grip)\b", re.I)
RE_NATUURBRAND = re.compile(
    r"\b(natuurbrand|bosbrand|heidebrand|duinbrand|(?:brand|br)\s*(?:\([^)]+\)\s*)?(?:natuur|bos|heide|duin|riet))\b",
    re.I,
)
RE_BIG_FIRE = re.compile(r"\b(middel\w*|grote|zeer\s*grote|grip)\b", re.I)
RE_BRANDMEESTER = re.compile(r"\bbrand\s*meester\b", re.I)
RE_DRONE = re.compile(
    r"\b(inzet\s*drone|droneteam|drone\s*team|team\s*digitale\s*verkenning|tdv|uas|rpas)\b",
    re.I,
)

RE_HMP = re.compile(r"\b(?:[LR]i?|Links|Rechts)?\s*\d+[.,]\d+\b", re.I)
RE_PREFIX_NOISE = re.compile(
    r"^(?:[AP]\s*\d+\s*)?(?:\([^)]+\)\s*)*(?:BNH-\d+\s*)?"
    r"(?:(?:Politie|Brandweer|Ambulance|AMBU(?:\s+\d+)?|Lifeliner|LL\s*[1-4]|MMT)\s+)?"
    r"(?:(?:naar|op|bij|nabij|langs|aan)\s+)?",
    re.I,
)
BLOCKED_WORDS = ("meldkamer", "brandweerkazerne", "ambulancepost")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sha(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius * math.asin(math.sqrt(min(1.0, a)))


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def strip_html(value: str) -> str:
    parser = _HTMLText()
    parser.feed(html.unescape(value or ""))
    return parser.text()


def fetch_url(url: str, timeout: int = 20) -> Optional[bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return response.read()
            LOG.warning("HTTP-status %s voor %s", response.status, url.split("?")[0])
    except Exception as exc:  # netwerkfouten mogen bestaande incidenten niet wissen
        LOG.warning("Ophalen mislukt (%s): %s", url.split("?")[0], exc)
    return None


def parse_feed(xml_bytes: bytes) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, Any]] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue

        def child_text(name: str) -> str:
            for child in list(item):
                if child.tag.rsplit("}", 1)[-1] == name:
                    return child.text or ""
            return ""

        title = strip_html(child_text("title"))
        desc = strip_html(child_text("description"))
        guid = (child_text("guid") or child_text("link") or title).strip()
        try:
            published = parsedate_to_datetime(child_text("pubDate"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        if guid and (title or desc):
            items.append({"guid": guid, "title": title, "desc": desc, "time": published})
    items.sort(key=lambda row: row["time"])
    return items


def classify(text: str) -> List[str]:
    categories: List[str] = []
    if RE_MMT.search(text):
        categories.append("mmt")
    if RE_GRIP_START.search(text) and not RE_GRIP_END.search(text):
        categories.append("grip")
    if RE_NATUURBRAND.search(text):
        categories.append("natuurbrand")
    if RE_DRONE.search(text):
        categories.append("drone")
    return categories


def extract_street_place(desc: str) -> Optional[Tuple[str, str]]:
    """Herkent zowel 'Politie naar X in Y' als 'BR natuur (heide) X in Y' en snelweg-hectometerpalen."""
    description = re.split(r"\s+voor\s+", desc, maxsplit=1, flags=re.I)[0].strip()
    if " in " not in description.lower():
        return None

    # Splits op de laatste ' in ' zodat plaatsnamen met meerdere woorden behouden blijven.
    match = re.match(r"^(.*)\s+in\s+(.+)$", description, re.I)
    if not match:
        return None
    left, place = match.group(1).strip(), match.group(2).strip()

    # Strip eventuele capcodes/dienstcodes achteraan de plaatsnaam (bijv. 'Soest 094131')
    place = re.sub(r"\s+\d{5,7}$", "", place).strip(" ,.")

    # Verwijder voorlooptekst (prio, eenheid, meldingssoort of voorzetsel)
    street = RE_PREFIX_NOISE.sub("", left)
    street = re.sub(
        r"^(?:(?:zeer\s+grote|grote|middel)?\s*(?:natuurbrand|bosbrand|heidebrand|brand|br)|grip\s*[1-4])\s*(?:\([^)]+\)\s*)?",
        "",
        street,
        flags=re.I,
    )
    # Strip hectometerpalen bijv. 'A1 Re 42,3' -> 'A1'
    street = RE_HMP.sub("", street).strip(" ,.-")

    if not street or not place or street.casefold() == place.casefold():
        return None
    if any(word in street.casefold() for word in BLOCKED_WORDS):
        return None
    return street, place


def pdok_geocode(street: str, place: str) -> Optional[Tuple[float, float]]:
    params = urllib.parse.urlencode([
        ("q", f"{street} {place}"),
        ("rows", "10"),
        ("fq", "type:weg"),
        ("fq", f'woonplaatsnaam:"{place}"'),
        ("fl", "straatnaam,woonplaatsnaam,centroide_ll,type"),
    ])
    raw = fetch_url(f"{PDOK_URL}?{params}")
    if not raw:
        raise RuntimeError("PDOK tijdelijk niet bereikbaar")
    try:
        documents = json.loads(raw.decode("utf-8")).get("response", {}).get("docs", [])
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Ongeldig antwoord van PDOK") from exc

    for document in documents:
        if document.get("type") != "weg":
            continue
        if str(document.get("woonplaatsnaam", "")).casefold() != place.casefold():
            continue
        match = re.fullmatch(r"POINT\(([-\d.]+) ([-\d.]+)\)", str(document.get("centroide_ll", "")))
        if match:
            lon, lat = float(match.group(1)), float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "incidents": [],
        "seen": {},
        "geo_cache": {},
        "retry": {},
        "last_feed_success": None,
    }


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Niet overschrijven: anders zouden actieve incidenten en deduplicatiestatus verdwijnen.
        raise RuntimeError(f"Statebestand is onleesbaar; publicatie afgebroken: {path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("incidents", []), list):
        raise RuntimeError("Statebestand heeft een onverwachte structuur; publicatie afgebroken")

    state.setdefault("incidents", [])
    state.setdefault("seen", {})
    state.setdefault("geo_cache", {})
    state.setdefault("retry", {})
    state.setdefault("last_feed_success", None)
    state["schema_version"] = SCHEMA_VERSION

    # Legacy state uit de vorige updater: seen[hash] was een ISO-string.
    for key, value in list(state["seen"].items()):
        if isinstance(value, str):
            state["seen"][key] = {"status": "processed", "at": value}
    return state


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def cached_geocode(
    street: str,
    place: str,
    state: Dict[str, Any],
    now: datetime,
    geocode: Callable[[str, str], Optional[Tuple[float, float]]],
) -> Optional[Tuple[float, float]]:
    key = sha(f"{street.casefold()}|{place.casefold()}")
    entry = state["geo_cache"].get(key)

    # Legacy cache met [lat, lon] blijft bruikbaar.
    if isinstance(entry, list) and len(entry) == 2:
        return float(entry[0]), float(entry[1])
    if isinstance(entry, dict):
        coords = entry.get("coords")
        if isinstance(coords, list) and len(coords) == 2:
            return float(coords[0]), float(coords[1])
        retry_after = entry.get("retry_after")
        if retry_after:
            try:
                if parse_iso(retry_after) > now:
                    return None
            except ValueError:
                pass

    try:
        result = geocode(street, place)
    except Exception as exc:
        LOG.warning("Geocodering tijdelijk mislukt: %s", exc)
        state["geo_cache"][key] = {
            "coords": None,
            "retry_after": iso(now + timedelta(minutes=10)),
        }
        raise

    if result is None:
        # Een niet-bestaande/onbekende straat niet permanent negatief cachen.
        state["geo_cache"][key] = {
            "coords": None,
            "retry_after": iso(now + timedelta(minutes=20)),
        }
        return None

    lat, lon = result
    # Afronding op ongeveer 100 m; deze afgeronde locatie wordt ook intern gebruikt.
    rounded = [round(float(lat), 3), round(float(lon), 3)]
    state["geo_cache"][key] = {"coords": rounded, "cached_at": iso(now)}
    return rounded[0], rounded[1]


def matching_window(cat: str) -> Tuple[int, int]:
    config = MATCHING[cat]
    return config["radius_m"], config["window_minutes"]


def find_incident(
    state: Dict[str, Any],
    cat: str,
    lat: float,
    lon: float,
    msg_time: datetime,
    now: datetime,
) -> Optional[Dict[str, Any]]:
    radius_m, window_minutes = matching_window(cat)
    candidates: List[Tuple[float, datetime, Dict[str, Any]]] = []
    for incident in state["incidents"]:
        if incident.get("cat") != cat:
            continue
        try:
            expires = parse_iso(incident["expires"])
            last = parse_iso(incident["last"])
        except (KeyError, ValueError):
            continue
        if expires <= now:
            continue
        if abs((msg_time - last).total_seconds()) > window_minutes * 60:
            continue
        distance = haversine_m(float(incident["lat"]), float(incident["lon"]), lat, lon)
        if distance <= radius_m:
            candidates.append((distance, last, incident))
    if not candidates:
        return None
    # Kies de dichtstbijzijnde match; bij gelijke afstand de meest recent bijgewerkte.
    candidates.sort(key=lambda candidate: (candidate[0], -candidate[1].timestamp()))
    return candidates[0][2]


def upsert_incident(
    state: Dict[str, Any],
    cat: str,
    lat: float,
    lon: float,
    msg_time: datetime,
    now: datetime,
) -> Dict[str, Any]:
    incident = find_incident(state, cat, lat, lon, msg_time, now)
    if incident is None:
        incident = {
            "id": sha(f"{cat}|{lat:.3f}|{lon:.3f}|{iso(msg_time)}"),
            "cat": cat,
            "lat": round(lat, 3),
            "lon": round(lon, 3),
            "radius": CATEGORIES[cat]["radius"],
            "first": iso(msg_time),
            "last": iso(msg_time),
            "expires": iso(msg_time),
            "grip_end": None,
        }
        state["incidents"].append(incident)
    if msg_time > parse_iso(incident["last"]):
        incident["last"] = iso(msg_time)
    return incident


def apply_ttl(incident: Dict[str, Any], msg_time: datetime) -> None:
    cat = incident["cat"]
    ttl = CATEGORIES[cat]["ttl_minutes"]
    proposed = msg_time + timedelta(minutes=ttl)
    current = parse_iso(incident["expires"])
    # Oude/laat binnengekomen meldingen mogen een incident niet terug in de tijd zetten.
    incident["expires"] = iso(max(current, proposed))


def mark_processed(state: Dict[str, Any], message_id: str, at: datetime, status: str = "processed") -> None:
    state["seen"][message_id] = {"status": status, "at": iso(at)}
    state["retry"].pop(message_id, None)


def schedule_retry(state: Dict[str, Any], message_id: str, now: datetime, reason: str) -> None:
    previous = state["retry"].get(message_id, {})
    attempts = int(previous.get("attempts", 0)) + 1
    delays = [2, 5, 10, 15, 20]
    delay = delays[min(attempts - 1, len(delays) - 1)]
    state["retry"][message_id] = {
        "attempts": attempts,
        "next_retry": iso(now + timedelta(minutes=delay)),
        "reason": reason[:80],
        "updated_at": iso(now),
    }


def process_items(
    items: List[Dict[str, Any]],
    state: Dict[str, Any],
    now: datetime,
    geocode: Callable[[str, str], Optional[Tuple[float, float]]] = pdok_geocode,
) -> Dict[str, int]:
    metrics = {
        "received": len(items),
        "relevant": 0,
        "geocoded": 0,
        "unlocated": 0,
        "retry_scheduled": 0,
        "processed": 0,
    }

    for item in items:
        message_id = sha(item["guid"])
        if message_id in state["seen"]:
            continue
        retry = state["retry"].get(message_id)
        if retry:
            try:
                if parse_iso(retry["next_retry"]) > now:
                    continue
            except (KeyError, ValueError):
                pass

        text = f'{item.get("title", "")} {item.get("desc", "")}'.strip()
        categories = classify(text)
        is_grip_end = bool(RE_GRIP_END.search(text))
        is_big_fire_update = (
            not categories
            and RE_NATUURBRAND.search(text) is None
            and re.search(r"\bbrand\b", text, re.I) is not None
            and (RE_BIG_FIRE.search(text) or RE_BRANDMEESTER.search(text))
        )
        if not categories and not is_grip_end and not is_big_fire_update:
            mark_processed(state, message_id, item["time"])
            metrics["processed"] += 1
            continue

        metrics["relevant"] += 1
        location_parts = extract_street_place(item.get("desc", ""))
        if location_parts is None:
            # Zonder bruikbare adresstructuur heeft opnieuw proberen geen zin.
            mark_processed(state, message_id, item["time"], "unlocated")
            metrics["unlocated"] += 1
            continue

        try:
            position = cached_geocode(*location_parts, state, now, geocode)
        except Exception as exc:
            schedule_retry(state, message_id, now, str(exc))
            metrics["retry_scheduled"] += 1
            continue

        if position is None:
            # Negatieve PDOK-hit kan tijdelijk zijn; probeer opnieuw zolang de RSS
            # de melding nog aanbiedt. De feedhistorie is beperkt.
            schedule_retry(state, message_id, now, "Geen exacte PDOK-match")
            metrics["retry_scheduled"] += 1
            continue

        lat, lon = position
        metrics["geocoded"] += 1
        msg_time = item["time"]

        if is_grip_end:
            grip = find_incident(state, "grip", lat, lon, msg_time, now)
            if grip:
                grip["grip_end"] = iso(msg_time)
                grip["expires"] = iso(msg_time + timedelta(minutes=15))
            mark_processed(state, message_id, msg_time)
            metrics["processed"] += 1
            continue

        # Aanvullende brandmeldingen kunnen een reeds bekende natuurbrand bijwerken.
        if is_big_fire_update:
            fire = find_incident(state, "natuurbrand", lat, lon, msg_time, now)
            if fire:
                if RE_BIG_FIRE.search(text):
                    fire["radius"] = BIG_FIRE_RADIUS_M
                if RE_BRANDMEESTER.search(text):
                    fire["expires"] = iso(msg_time + timedelta(minutes=BRANDMEESTER_TTL_MINUTES))
                else:
                    apply_ttl(fire, msg_time)
                fire["last"] = iso(max(msg_time, parse_iso(fire["last"])))
                mark_processed(state, message_id, msg_time)
                metrics["processed"] += 1
                continue

        for category in categories:
            incident = upsert_incident(state, category, lat, lon, msg_time, now)
            if category == "grip":
                incident["grip_end"] = None
            if category == "natuurbrand":
                if RE_BIG_FIRE.search(text):
                    incident["radius"] = BIG_FIRE_RADIUS_M
                if RE_BRANDMEESTER.search(text):
                    incident["expires"] = iso(msg_time + timedelta(minutes=BRANDMEESTER_TTL_MINUTES))
                else:
                    apply_ttl(incident, msg_time)
            else:
                apply_ttl(incident, msg_time)

        mark_processed(state, message_id, msg_time)
        metrics["processed"] += 1

    # Beperk state-groei. Pending retries vervallen na 2 uur; RSS-items zijn dan
    # normaliter ook niet meer beschikbaar.
    retry_cutoff = now - timedelta(hours=2)
    state["retry"] = {
        key: value
        for key, value in state["retry"].items()
        if parse_iso(value.get("updated_at", iso(now))) > retry_cutoff
    }
    seen_cutoff = now - timedelta(days=2)
    state["seen"] = {
        key: value
        for key, value in state["seen"].items()
        if isinstance(value, dict) and parse_iso(value.get("at", iso(now))) > seen_cutoff
    }
    return metrics


def finalize(state: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    # Geen automatische drone-koppeling op basis van alleen nabijheid.
    # Zonder gemeenschappelijke incident-ID is zo'n relatie niet betrouwbaar.
    active = []
    for incident in state["incidents"]:
        try:
            if parse_iso(incident["expires"]) > now:
                active.append(incident)
        except (KeyError, ValueError):
            LOG.warning("Ongeldig incident uit state verwijderd")
    state["incidents"] = active

    public_incidents: List[Dict[str, Any]] = []
    for incident in active:
        category = incident.get("cat")
        if category not in CATEGORIES:
            continue
        lat, lon = float(incident["lat"]), float(incident["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            LOG.warning("Ongeldige coördinaten; incident niet gepubliceerd")
            continue
        public_incidents.append({
            "id": incident["id"],
            "category": category,
            "label": CATEGORIES[category]["label"],
            "lat": round(lat, 3),
            "lon": round(lon, 3),
            "radius_m": int(incident["radius"]),
            "updated_at": incident["last"],
            "expires_at": incident["expires"],
        })
    return public_incidents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory met state.json en incidents.json")
    args = parser.parse_args()
    output_dir = Path(args.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "state.json"
    output_path = output_dir / "incidents.json"

    now = utc_now()
    state = load_state(state_path)

    raw = fetch_url(FEED_URL)
    feed_ok = False
    metrics = {
        "received": 0,
        "relevant": 0,
        "geocoded": 0,
        "unlocated": 0,
        "retry_scheduled": 0,
        "processed": 0,
    }

    if raw is not None:
        try:
            items = parse_feed(raw)
            metrics = process_items(items, state, now)
            feed_ok = True
            state["last_feed_success"] = iso(now)
        except ET.ParseError as exc:
            LOG.warning("RSS-feed onleesbaar: %s", exc)
    if not feed_ok:
        LOG.warning("RSS-feed niet succesvol verwerkt; bestaande incidenten behouden tot hun TTL.")

    incidents = finalize(state, now)
    last_success = state.get("last_feed_success")

    # Publiceer beide bestanden atomair. State eerst: als publicatie van incidents
    # daarna faalt, kan de volgende run de meldingen nog steeds dedupliceren.
    atomic_write_json(state_path, state)
    public_payload = {
        "generated_at": iso(now),
        "feed_ok": feed_ok,
        "last_feed_success": last_success,
        "metrics": metrics,
        "incidents": incidents,
    }
    atomic_write_json(output_path, public_payload)

    LOG.info(
        "Klaar: %d actieve incidenten | feed_ok=%s | ontvangen=%d | relevant=%d | "
        "geocodeerd=%d | retry=%d | niet_geplaatst=%d",
        len(incidents),
        feed_ok,
        metrics["received"],
        metrics["relevant"],
        metrics["geocoded"],
        metrics["retry_scheduled"],
        metrics["unlocated"],
    )


if __name__ == "__main__":
    main()
```[cite: 1]
