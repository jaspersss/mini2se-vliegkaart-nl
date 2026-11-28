#!/usr/bin/env python3
"""Tests voor de P2000-incidentlogica (synthetische meldingen, gemockte geocoder)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_incidents as ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("OK:   " if cond else "FAIL: ") + msg)
    if not cond:
        failures.append(msg)


T0 = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
# Gemockte geocoder: bekende straten, "Onbekendeweg" -> geen resultaat
GEO = {("Heideweg", "Ede"): (52.04, 5.66), ("Dorpsstraat", "Ede"): (52.045, 5.665), ("Veraweg", "Soest"): (52.15, 5.30)}
geocode = lambda s, p: GEO.get((s, p))  # noqa: E731


def item(minutes, desc, title=""):
    return {"guid": f"g{minutes}{desc}", "title": title or desc.lower(), "desc": desc, "time": T0 + timedelta(minutes=minutes)}


def run(items, now_min):
    st = {"incidents": [], "seen": {}, "geo_cache": {}}
    now = T0 + timedelta(minutes=now_min)
    ui.process_items(items, st, now, geocode)
    return st, ui.finalize(st, now)


# --- Classificatie
check(ui.classify("Lifeliner 3 naar X") == ["mmt"], "MMT-trigger (Lifeliner)")
check(ui.classify("GRIP 2 incident") == ["grip"], "GRIP 1-4 start")
check(ui.classify("einde grip 2") == [], "einde GRIP is geen start")
check(ui.classify("bermbrand A12") == [], "kleine bermbrand wordt genegeerd")
check(ui.classify("Natuurbrand Heideweg") == ["natuurbrand"], "natuurbrand-trigger")
check(ui.classify("Inzet drone vermissing") == ["drone"], "drone-trigger")
check(ui.classify("ambulance met spoed naar Heideweg in Ede") == [], "gewone melding genegeerd")

# --- Parser / geocoding-check
check(ui.extract_street_place("Politie naar Provincialeweg in Ingen voor ongeval met letsel") == ("Provincialeweg", "Ingen"), "straat+plaats uit omschrijving")
check(ui.extract_street_place("Brandweer in Ede") is None, "geen straat -> geen locatie (geen plaatsmiddelpunt)")
check(ui.extract_street_place("Brandweer naar Ede in Ede") is None, "straat gelijk aan plaats geweigerd")

# --- MMT: 90 min vaste TTL
st, out = run([item(0, "Lifeliner naar Heideweg in Ede")], 60)
check(len(out) == 1 and out[0]["radius_m"] == 2000, "MMT actief met 2km")
check(out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=90)), "MMT TTL = laatste bericht + 90 min")
st, out = run([item(0, "Lifeliner naar Heideweg in Ede")], 91)
check(out == [], "MMT verloopt na 90 min")

# --- Groepering binnen 1,5 km (Dorpsstraat ~0,6km van Heideweg) verlengt hetzelfde incident
st, out = run([item(0, "Lifeliner naar Heideweg in Ede"), item(30, "MMT naar Dorpsstraat in Ede")], 100)
check(len(out) == 1 and out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=120)), "groepering: 1 incident, TTL verlengd")
st, out = run([item(0, "Lifeliner naar Heideweg in Ede"), item(5, "MMT naar Veraweg in Soest")], 10)
check(len(out) == 2, "ver uit elkaar = 2 incidenten")

# --- GRIP: eind-trigger +15 min, fallback 6 uur
st, out = run([item(0, "GRIP 1 brand naar Heideweg in Ede")], 300)
check(len(out) == 1 and out[0]["expires_at"] == ui.iso(T0 + timedelta(hours=6)), "GRIP fallback-TTL 6 uur")
st, out = run([item(0, "GRIP 1 brand naar Heideweg in Ede"), item(60, "einde grip naar Heideweg in Ede")], 70)
check(len(out) == 1 and out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=75)), "GRIP einde -> +15 min")
st, out = run([item(0, "GRIP 1 brand naar Heideweg in Ede"), item(60, "einde grip naar Heideweg in Ede")], 80)
check(out == [], "GRIP verdwenen 15 min na einde")

# --- Natuurbrand: 3u sliding, 4,5km bij opschaling, brandmeester -> 90 min
st, out = run([item(0, "Natuurbrand naar Heideweg in Ede")], 10)
check(out[0]["radius_m"] == 2000 and out[0]["expires_at"] == ui.iso(T0 + timedelta(hours=3)), "natuurbrand 2km, 3u TTL")
st, out = run([item(0, "Natuurbrand naar Heideweg in Ede"), item(20, "Middelbrand natuurbrand naar Dorpsstraat in Ede")], 30)
check(out[0]["radius_m"] == 4500, "opschaling middel -> 4,5km")
check(out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=20) + timedelta(hours=3)), "sliding TTL reset bij nieuw bericht")
st, out = run([item(0, "Natuurbrand naar Heideweg in Ede"), item(30, "Grote brand: brandmeester naar Dorpsstraat in Ede")], 40)
check(out[0]["radius_m"] == 4500 and out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=120)), "gekoppeld bericht: radius 4,5km + brandmeester -> 90 min")

# --- Drone: 2,5u, of mee met langer lopend gekoppeld incident
st, out = run([item(0, "Inzet drone naar Heideweg in Ede")], 10)
check(out[0]["expires_at"] == ui.iso(T0 + timedelta(minutes=150)), "drone TTL 2,5u")
st, out = run([item(0, "Natuurbrand naar Heideweg in Ede"), item(5, "Droneteam naar Dorpsstraat in Ede")], 10)
d = [o for o in out if o["category"] == "drone"][0]
check(d["expires_at"] == ui.iso(T0 + timedelta(hours=3)), "drone loopt mee met langer natuurbrand-incident")

# --- Geen locatie = geen cirkel
st, out = run([item(0, "Lifeliner naar Onbekendeweg in Ede")], 10)
check(out == [], "niet te geocoderen -> geen incident")

# --- Privacy: geen straat/ruwe tekst in publieke output of state
st, out = run([item(0, "Lifeliner naar Heideweg in Ede")], 10)
import json  # noqa: E402
blob = json.dumps(out) + json.dumps(st)
check("Heideweg" not in blob and "Lifeliner" not in blob and "Ede" not in blob, "geen straatnaam/ruwe tekst in output of state")
check(out[0]["lat"] == round(out[0]["lat"], 3), "coördinaten ~100m afgerond")

# --- Idempotent: dezelfde melding twee keer verwerken verandert niets
st = {"incidents": [], "seen": {}, "geo_cache": {}}
now = T0 + timedelta(minutes=10)
its = [item(0, "Lifeliner naar Heideweg in Ede")]
ui.process_items(its, st, now, geocode)
a = json.dumps(st["incidents"])
ui.process_items(its, st, now, geocode)
check(a == json.dumps(st["incidents"]), "dezelfde melding opnieuw verwerken = geen wijziging")

print(f"\n{len(failures)} fout(en)")
sys.exit(1 if failures else 0)
