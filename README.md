# 🛸 Dronekaart van Jasper (EU Open Categorie A1 / A2 / A3)

Een lichtgewicht, mobiel-geoptimaliseerde Progressive Web App (PWA) voor dronepiloten in Nederland met ondersteuning voor **12 droneprofielen** (C0 `< 250g` Open A1 zoals de DJI Mini 2 SE / Mini 4 Pro, C1 Open A1, C2 Open A2 en Legacy `> 250g` Open A3).

🔗 **Live Web-App:** [https://drone.jasperneeleman.nl](https://drone.jasperneeleman.nl)

---

## 🎯 Waarom deze app?
Bestaande dronekaarten tonen vaak algemene meldingen die per dronecategorie verschillen of geven onduidelijke Natura 2000-waarschuwingen. **Dronekaart van Jasper** combineert officiële GoDrone/LVNL-data, PDOK Natura 2000-gebieden en OpenStreetMap-infrastructuur in een eigen beslis-engine (`evaluateLocationAndTime`) en geeft bovenaan het scherm direct antwoord op de vraag:
> *"Welke beperkingen of voorwaarden gelden op déze plek, op dít tijdstip en voor míjn drone?"*

*(Let op: deze app is een hulpmiddel en geeft geen juridische vluchtgoedkeuring. Controleer bij twijfel altijd GoDrone / LVNL.)*

---

## ✨ Belangrijkste Functies

1. **Drone-specifieke Beslis-Engine (`evaluateLocationAndTime(lngLat, timestamp)`):**
   - 🔴 **NO-FLY (VERBODEN `0m`):** CTR Zone 1 & 2, militaire CTR's, beveiligde gebieden, actieve NO-FLY NOTAMs/TGB's, Natura 2000-gebieden met expliciet droneverbod, buiten het wettelijke Nederlandse daglichtvenster (UDP), en voor **A3** ook actieve laagvlieggebieden of woon-/industrie-/recreatiegebieden binnen **150m**.
   - 🟠 **LET OP / VOORWAARDEN:** Laagvlieggebieden (max 30m voor A1/A2), Natura 2000 (zorgplicht, >50–100m van vogels, niet opstijgen vanaf Staatsbosbeheer/Natuurmonumenten zonder toestemming), Nationale Parken, waarschuwings-NOTAMs, of een verouderde NOTAM-feed (>3 uur oud).
   - 🟢 **GEEN BEPERKINGEN GEVONDEN (`Max. 120m AGL`):** Geen beperking gevonden in de beschikbare data (o.a. civiele CTR Zone 3 buitenring tot 120m).
2. **Atomaire Data-Pipeline + Versheidscontrole (`scripts/update_geodata.py` & `data/meta.json`):**
   - Draait elk uur via GitHub Actions en haalt pas nieuwe GoDrone-data over als **alle 12 geografische tegels** succesvol zijn opgehaald.
   - Houdt per databron (`godrone_luchtruim`, `godrone_notams`, `natura2000`) de status (`ok`, `cached`, `error`) en `last_success` bij. Bij verouderde NOTAM-data (>3 uur) toont de app automatisch een waarschuwing.
   - Draait vóór elke publicatie automatisch de regressietests in [`tests/test_locations.py`](./tests/test_locations.py).
3. **Mobile-First Veld-Interface:**
   - **Sticky Top Status Banner** met maximale vlieghoogte en live daglicht-aftelklok (`SunCalc.js`).
   - **~500m VLOS-richtlijncirkel** (`Turf.js`) rond je live GPS-positie of het kaart-kruisdraad.
   - **Tijd-simulatie balk** (`Nu`, `+1 uur`, `+3 uur`, `Morgen 10:00` + tijdslider) om vooruit te kijken.

---

## ⚙️ GitHub Actions & Secret Instellen

1. **API-sleutel als GitHub Secret (`GODRONE_API_KEY`):**
   De GoDrone API-sleutel staat niet meer in de broncode. Stel hem in op GitHub via **Settings > Secrets and variables > Actions > New repository secret** met naam `GODRONE_API_KEY`.
2. **GitHub Actions Write Permissions:**
   Ga naar **Settings > Actions > General > Workflow permissions** en kies **"Read and write permissions"**.
3. **GitHub Pages:**
   Ga naar **Settings > Pages**, kies **Deploy from a branch** (`main`, `/ (root)`).

---

## 🗂️ Eigen Natura 2000-beoordelingen toevoegen
Pas [`data/n2000_overrides.json`](./data/n2000_overrides.json) aan om voor een specifiek `gebiedsnummer` de status (`forbidden`, `caution`, `free`) en een heldere praktijktoelichting toe te voegen. Zodra je dit bestand commit, draait de GitHub Action automatisch om `data/natura2000_slim.geojson` bij te werken.
