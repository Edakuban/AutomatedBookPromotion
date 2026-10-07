# Einfacher Bildflow – Umsetzungsplan für 05.10.2026

Dieser ursprüngliche Zwei-Button-Plan ist durch die spätere Drei-Button-Auswahl
ergänzt worden. Aktueller Umfang und Modellpakete: [Globale KI-Auswahl](ai-image-presets.md).

Stand: Umsetzung vom 05.10.2026. Gemeinsamer UI-/Queue-Ablauf implementiert;
die visuelle Identitätsabnahme ist ausdrücklich noch nicht abgeschlossen.

## Ziel und Oberfläche

Bei Zitaten und Kapiteln derselbe sichtbare Ablauf:

1. Szene aus Zitat/Kapitelanalyse, bearbeitbar.
2. Charaktere auswählen; optional Pose, Ausdruck und Requisiten vorgeben.
3. Zwei Hauptaktionen:
   - **Bild erzeugen:** Szene + Buchstil + Aussehensbeschreibungen aus den
     vorhandenen Charakter-Imageprompts; keine Referenzbilder.
   - **Bild mit meinen Charakteren erzeugen:** dieselbe Szene + Buchstil +
     hinterlegte Referenzbilder; Beschreibungen ergänzen nur Identitätsmerkmale.
4. Ergebnis prüfen und auswählen; danach Video/Carousel wie bisher.

Kein vorheriges Szenenbild als Voraussetzung. Kein zusätzlicher Pflichtprompt,
kein manuelles Planspeichern zwischen Hauptaktionen. Technische Verfahren,
Szenenplan und effektiver Renderprompt bleiben unter „Erweiterte Einstellungen“.
Alle bestehenden Funktionen und bisherigen Verfahren bleiben erreichbar.

## 1. ComfyUI-Basis prüfen

- Aktueller Referenzstack entspricht den offiziellen Komponenten: FLUX.2 Klein
  Base 9B FP8, Qwen3 8B, FLUX.2 small-decoder, native ReferenceLatent-Ketten.
- Die App baut einen eigenen Rendergraph. Deshalb offiziellen Workflow als
  kontrollierte Vergleichsbasis prüfen: erst eine Figur mit neuer Pose und großer
  Darstellung, anschließend zwei Figuren und eine konkrete Buchszene.
- Ungefähr ein Megapixel; reine Vergrößerung eines fertigen Bildes zählt nicht als
  Identitätsverbesserung. Modell, Seed, Referenzen und Einstellungen dokumentieren.
- Der bisherige A/B-Test mit direkter Bildnummernzuordnung war kein brauchbarer
  Fix; B erzeugte sogar eine dritte Figur. Nicht ungeprüft übernehmen.
- Modellwechsel erst bei belegtem Bedarf. 9B-Lizenz vor kommerzieller Nutzung
  separat prüfen. Keine externen Bilddienste ohne entsprechende Freigabe.

## 2. Gemeinsamen Backend-Auftrag bauen

- Ein gemeinsamer Einstieg für beide Bildvarianten und beide Inhaltsarten.
  Zitate liefern Zitat plus Nahkontext; Kapitel liefern den ausgewählten belegten
  Moment. Charakterprofile bleiben buchbezogen, ohne Sam-/Lys-Sonderregeln.
- Vorhandenen gültigen Szenenplan wiederverwenden. Falls nötig, im selben
  Benutzerauftrag automatisch einen Plan mit dem gewählten Text-KI-Provider
  erzeugen. Hintergrund-KI ist ausdrücklich erlaubt, aber keine stillen
  Providerwechsel zu kostenpflichtigen/externalen Diensten.
- Identität, Szene und Stil getrennt halten: Referenzpose, Hintergrund und
  gehaltene Waffen nicht als Charaktermerkmale übernehmen. Neue Pose und neuer
  Ausdruck kommen aus der Szene, Gesicht/Körperbau/Anatomie/Kleidung aus der Referenz.
- Referenzmodus rendert ohne vorhandenes Rohbild; Zielgröße explizit bestimmen.
  Fehlende Referenzen klar melden, nicht unbemerkt auf generische Figuren wechseln.
- Queue mehrstufig und wiederaufnehmbar: Planung → Bildlauf → Ergebnis.
  Laufende Jobs, Revisionen, Referenzhashes und veraltete Eingaben absichern.
  Ein Bildklick darf nicht doppelte Jobs oder unbegrenzte KI-Retries erzeugen.

## 3. UI für Zitate und Kapitel vereinheitlichen

- Zwei Hauptbuttons, gleiche Beschriftungen, Charakterauswahl und Dropdown-Stile.
- Fortschritt verständlich zeigen: „Szene vorbereiten“, „Bild erzeugen“, „Fertig“;
  Anbieter/Kostenhinweise und Fehler bleiben sichtbar.
- Szenenplan-Aktualisierung automatisch im Auftrag; dessen Editor optional.
- Ergebnisse als auswählbare Varianten erhalten. Bestehende Bild-/Videoauswahl
  nicht automatisch ersetzen. Carousel-Vorbereitungen und Publishing erhalten.
- Alte Entwürfe, Queue-Aufträge und Maskenverfahren kompatibel halten.

## 4. Abnahme

- Backend-/UI-Tests für beide Quellen und Modi, fehlende Referenzen, veraltete
  Pläne, Fehler/Retry, Mehrfachklicks und Erhaltung vorhandener Medien.
- Sichtprüfung mit einer und mehreren Figuren sowie unterschiedlichen Buchstilen
  und nichtmenschlichen Formen. Richtige Figurenanzahl, neue Pose/Ausdruck,
  wiedererkennbares Gesicht, Körperbau, Anatomie und Kleidung.
- UI-Vereinfachung und Bildqualität getrennt bewerten: grüne Softwaretests sind
  kein Beleg für erfolgreiche Identitätsübertragung.
- Vor Backend-Neustart leere Queue und exakten eigenen Prozess prüfen. Vorhandene
  uncommittete Arbeit erhalten; keine fremden Änderungen zurücksetzen.

Reihenfolge: kurzer Workflow-Basistest → gemeinsamer Backend-Auftrag → beide UIs
vereinfachen → Regressionstests und echte Bildabnahme. Falls die Referenzqualität
weiter scheitert, Befund offen dokumentieren und den Renderadapter gezielt ändern.

## Umsetzungsstand 05.10.2026

- Zitat-Werkstatt und Kapitel-Teaser teilen dieselbe Eingabe und dieselben beiden
  Hauptaktionen. Szene, Buchcharaktere und optionale Regie werden mit dem Bildklick
  atomar gespeichert; kein vorheriges Bild oder manuelles Planspeichern nötig.
- `image_flow.py` bündelt Planung und beschreibungsbasierte Konditionierung;
  `ReelJobStore` und der Reel-Worker verarbeiten beide Quellen. Kapitel bleiben
  nach dem Bildauftrag im manuellen Review, ohne automatisch Videos nachzuschieben.
- Gewählten Text-KI-Provider samt Modell/Endpoint einfrieren, gültigen Plan
  wiederverwenden, keine stillen Providerwechsel. Referenzmodus prüft alle
  Referenzdateien/Hashes und das native Profil vor einer optionalen Planung.
- Fortschritt und wiederaufnehmbare Comfy-Prompt-ID werden dauerhaft gespeichert.
  Unklare Unterbrechungen lösen keinen zweiten KI-Aufruf aus; bekannte Bildläufe
  werden nur gepollt. Der tatsächliche Rendertext wird vor dem Absenden eingefroren,
  damit ein Codeupdate beim Resume nicht rückwirkend den Promptaudit verändert.
- Revisionen, Quelle, Buchstil und Referenzen vor Render und Ergebnisübernahme
  prüfen. Veraltete Ergebnisse bleiben in der Jobhistorie, ersetzen keine Kandidaten.
- Vorhandene Auswahl/Bilder/Videos erhalten. Charakter-Snapshot und Bewegungs-KI
  beziehen sich auf die tatsächlich ausgewählte Bildversion, nicht den letzten Versuch.
  Alte Verfahren, Uploads, Carousel-Zuschnitt/-Vorbereitung und Publishing bleiben da.
- Backend-/Browser-/JavaScript-Regressionen sowie echte lokale GPU-Läufe getrennt
  geprüft. Produktionsbücher wurden für die Bildvergleiche nur gelesen; alle
  Testmedien und Jobs liegen in isolierten Testdaten.

### Grenzen der Bildabnahme

Der Komponentenvergleich mit einer und zwei Figuren zeigt: native Klein-Referenzen
und neue Posen funktionieren grundsätzlich, exakte Gesicht-/Outfitübertragung aber
nicht zuverlässig. Der echte neue Zweifiguren-Flow plante korrekt zwei Akteure,
zeichnete jedoch eine dritte Figur. Deshalb ist „Identität zuverlässig gelöst“
kein abgeschlossener Punkt, auch bei vollständig grünen Softwaretests.

Zwei erste Prompt-Folgevergleiche sind **ungültig**: Im Experimentskript waren die
Bildnummern gegen die tatsächliche Worker-/Graphreihenfolge vertauscht. Die App
ordnet Referenzen korrekt zu. Aus diesen Vergleichen wird keine Renderänderung
abgeleitet; weitere Qualitätsaussagen brauchen korrekt gehashte Bildzuordnungen.

Der anschließend **korrekt gebundene** Vergleich (gleicher Graph, Seed, Modell,
Referenzpixel und Sampling; nur Rendertext verändert) ergab genau zwei Figuren
und erhielt die nichtmenschlichen Merkmale. Daher verwendet nur der neue einfache
Referenzmodus jetzt einen gemeinsamen Image-/Appearance-/Pose-Block je Akteur.
Bildnummern folgen den tatsächlichen Referenzpixeln, nicht der Akteurs- oder
Profilauflistung. Der deployte Compiler wurde gegen den exakt gerenderten Text
und die gehashten Bildbindungen verifiziert. Alte Compiler bleiben erreichbar.

Offen bleiben exakte Gesichter/Kleidung und Planbefolgung: Die zweite Figur sitzt
wach statt zu schlafen, ihr Outfit weicht ab. Das ist eine begrenzte Verbesserung,
keine bestandene globale Identitätsabnahme. Mehrere Buchstile sind softwareseitig
abgedeckt, aber noch nicht als echte GPU-Bildserie visuell abgenommen.
