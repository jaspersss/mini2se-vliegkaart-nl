# 🛸 Mini2SE Vliegkaart NL (`< 250g` EU Open Categorie A1)

Een lichtgewicht, mobiel-geoptimaliseerde Progressive Web App (PWA) specifiek ontworpen voor dronepiloten met een **DJI Mini 2 SE (< 250 gram, EU Open Categorie A1)** in Nederland.

🔗 **Live Web-App (GitHub Pages):** [https://jaspersss.github.io/mini2se-vliegkaart-nl/](https://jaspersss.github.io/mini2se-vliegkaart-nl/)

---

## 🎯 Waarom deze app?
Bestaande dronekaarten (zoals GoDrone en Aeret) tonen veel juridische ruis die niet geldt voor een `< 250g` A1-drone (zoals A2/A3-afstandseisen of op dat moment inactieve militaire laagvlieggebieden) en geven vage meldingen bij Natura 2000-gebieden (*"Raadpleeg het beheerplan"*).

**Mini2SE Vliegkaart NL** filtert alle data door een **DJI Mini 2 SE regel-engine** en geeft bovenaan het scherm direct één eenduidig antwoord op de vraag:
> *"Mag ik op déze plek en op dít moment opstijgen en vliegen?"*

---

## ✨ Belangrijkste Functies

1. **Beslis-Engine (`evaluateLocationAndTime(lngLat, timestamp)`):**
   - 🔴 **NO-FLY (`0m`):** Binnenste civiele/militaire CTR's, actieve NOTAMs/TGB's, Provinciale Stiltegebieden, Natura 2000-gebieden met een expliciet vliegverbod én buiten het wettelijke Nederlandse daglichtvenster (15 min vóór zonsopkomst tot 15 min ná zonsondergang).
   - 🟠 **LET OP / VOORWAARDEN:** Natura 2000 *"Grijs Gebied"* (zorgplicht fauna, >50-100m afstand van vogels, opstijgverbod vanaf terrein Staatsbosbeheer/Natuurmonumenten zonder toestemming), actieve tijdgebonden zones (GLV / zweefvliegsectoren) en buitenste civiele CTR-ringen.
   - 🟢 **VRIJ OM TE VLIEGEN (`Max. 120m AGL`):** Geen actieve restricties op deze locatie en dit tijdstip (inclusief duidelijk bericht wanneer een laagvlieggebied of zweefvliegzone op dat moment *inactief* is).
2. **Oplossing voor het "Natura 2000 Vaagheid-probleem":**
   - Combineert PDOK Natura 2000-geometrieën via een **Spatial Join** met Provinciale Stiltegebieden en onze eigen kennisbank in [`data/n2000_overrides.json`](./data/n2000_overrides.json).
3. **Mobile-First Veld-Interface:**
   - **Sticky Top Status Banner** met maximale vlieghoogte en live daglicht-aftelklok (`SunCalc.js`).
   - **500m VLOS-referentiecirkel** (`Turf.js`) rond je live GPS-positie of het kaart-kruisdraad (*"Check dit punt"*).
   - **Tijd-simulatie balk** (`Nu`, `+1 uur`, `+3 uur`, `Morgen 10:00` + tijdslider) om vooruit te kijken.

---

## ⚙️ GitHub Pages & GitHub Actions Instellingen

### 1. GitHub Actions Write Permissions
Zodat de workflow [`.github/workflows/update_data.yml`](./.github/workflows/update_data.yml) periodiek zelf de verrijkte en vereenvoudigde GeoJSON-bestanden in `/data/` kan committen:
1. Ga op GitHub naar **Settings > Actions > General**.
2. Scroll naar **Workflow permissions**.
3. Selecteer **"Read and write permissions"** en klik op **Save**.

### 2. GitHub Pages Activeren
1. Ga op GitHub naar **Settings > Pages**.
2. Kies onder **Build and deployment > Source** voor **Deploy from a branch**.
3. Selecteer branch `main` en folder `/ (root)` en klik op **Save**.

---

## 🗂️ Eigen Natura 2000-beoordelingen toevoegen
Pas eenvoudig [`data/n2000_overrides.json`](./data/n2000_overrides.json) aan om voor een specifiek `gebiedsnummer` de status (`forbidden`, `caution`, `free`) en een heldere praktijktoelichting toe te voegen. Zodra je dit bestand commit, draait de GitHub Action automatisch om `data/natura2000_slim.geojson` bij te werken.
