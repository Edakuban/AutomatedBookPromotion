# Automated Book Promotion

Schlichte lokale Verwaltung eigener Bücher mit einer FastAPI-Weboberfläche, Word-Import und KI-Zitatauswahl über Open WebUI. Supabase ist die gemeinsame Datenbasis; zwei importierbare n8n-Workflows übernehmen Texte, Carousel-Bilder, optional getrennte Telegram-Freigaben und Instagram-Posts.

## Aktueller Stand

**Lokale Assets, CTA-Renderer, Python-Sync, Supabase-Vertrag v5 sowie die lokalen Review- und Auto-Carousel-Exporte sind umgesetzt und strukturell getestet.** Das Zielprojekt läuft auf v5. Vor der Aktivierung stehen noch der n8n-Import sowie Review-Dry-Run und Livetest aus. Der bisherige Einzelbild-Export wurde ohne Kompatibilitätsschicht entfernt.

Der Upload funktioniert ohne Supabase und legt lokale Buchprojekte an. Ein Klick auf das Buch zeigt Datei, Kapitelaufteilung, Kapitelübersicht und KI-Analyse. Den Text findest du innerhalb der Kapitel. Kapitelgrenzen lassen sich korrigieren und bestätigen. Anschließend lässt sich die KI-Analyse starten; fertige Zitate stehen auf den Kapitelseiten. Vorhandene Supabase-Bücher werden nach Aktivierung separat angezeigt.

Einrichtung: [Supabase](sql/README.md), [Datenübertragung](docs/supabase-integration.md) und [n8n-Import mit Credentials und Testablauf](n8n/README.md). Importdateien: [Review](n8n/book-promotion-review.json) und [Auto](n8n/book-promotion-auto.json).

Der ausführliche Ablauf steht in [IMPLEMENTIERUNGSPLAN.md](IMPLEMENTIERUNGSPLAN.md).

## Einrichtung unter Windows

Voraussetzung: [uv](https://docs.astral.sh/uv/getting-started/installation/) und Python ab 3.11. Die Entwicklung wird mit Python 3.12 geprüft. Falls nötig kann `uv python install 3.12` Python bereitstellen.

Im Projektordner in PowerShell ausführen:

```powershell
uv sync --locked --python 3.12
if (!(Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
.\check-config.bat
```

`uv sync --locked` installiert die in `uv.lock` festgehaltenen Abhängigkeiten in `.venv`. Eine vorhandene `.env` wird durch die obige Einrichtung nicht überschrieben. Auch ohne `.env` läuft die Basisprüfung mit lokalen Standardwerten.

## Weboberfläche starten

Nach der Einrichtung `start.bat` doppelklicken oder in PowerShell ausführen:

```powershell
.\start.bat
```

Das Skript verwendet immer den Projektordner und startet Webserver und Import-Worker mit den lokalen Einstellungen aus `.env`. Nach erfolgreichem Serverstart öffnet sich der Browser, standardmäßig unter [http://127.0.0.1:8000](http://127.0.0.1:8000). Das Terminal bleibt geöffnet; **Strg+C** beendet Server und Worker. Das Schließen des Browsers beendet die Verarbeitung nicht.

```powershell
# Ohne automatisches Browserfenster:
.\start.bat --no-browser

# Andere Konfigurationsdatei:
.\start.bat --env-file .env.local
```

Unter `/` liegt die Buchübersicht, unter `/settings` der Einrichtungsstatus. `/health` bestätigt den laufenden Webserver. Bei deaktivierter oder fehlerhafter Datenbankverbindung meldet es `database_connected: false`; bei aktivierter Verbindung prüft es zusätzlich Schemaversion und Lesezugriff auf alle vorgesehenen Objekte.

Die Karten der Einstellungsseite zeigen die Vollständigkeit der Konfiguration. Darunter lassen sich Open-WebUI-Zugang, Modellwahl und eine technische Testantwort prüfen. Nur bei `SUPABASE_ENABLED=true` greift die Buchübersicht lesend auf Supabase zu. Manuelle Änderungen an `.env` erfordern einen Neustart; eine über die Oberfläche gespeicherte Modellwahl gilt sofort für neue KI-Anfragen. Private Dateien und das Datenverzeichnis werden nicht als Webinhalte bereitgestellt.

Optionaler n8n-API-Zugang für Workflow- und Ausführungsdiagnosen steht ebenfalls in dieser zentralen `.env`: `N8N_URL` und `N8N_API_KEY`. Beide Einträge dürfen leer bleiben und lösen beim App-Start keine n8n-Anfragen aus.

## Word-Dokument hochladen (Schritt 4)

1. Anwendung mit `start.bat` starten und „Bücher“ öffnen.
2. Eine `.docx` in den Upload-Bereich ziehen. Alternativ eine Datei auswählen und „Buch anlegen“ drücken.
3. Nach Upload und Dateiprüfung öffnet sich das lokale Buchprojekt. Die Textextraktion läuft im Hintergrund; den Fortschritt zeigt die Buchseite automatisch an. Der vorläufige Titel stammt aus dem Dateinamen.
4. Über „Bücher“ zur Übersicht zurückkehren. Das Projekt bleibt auch nach einem Neustart erhalten.

Standardlimit: 25 MB pro Datei. Fortschritt und Fehler erscheinen im Upload-Bereich. Geprüft werden das ZIP-/Word-Paket, sein Hauptdokument, XML-Lesbarkeit und CRC-Prüfsummen. Alte `.doc`-Dateien, PDFs, leere, beschädigte, verschlüsselte oder umbenannte Makrodateien werden abgelehnt. Größere entpackte Pakete sind auf 100 MB, einzelne benötigte XML-Dateien auf 20 MB begrenzt.

Identische Dateien werden anhand von SHA-256 erkannt, auch bei anderem Dateinamen oder gleichzeitigem Upload. Sie öffnen dasselbe Projekt. Eine inhaltlich/gepackt veränderte Datei erzeugt in diesem Schritt ein neues Projekt; die gezielte Aktualisierung einer bestehenden Buchversion folgt später.

Die Ablage besteht aus:

- `data/originals/<version-id>.docx`: unveränderte Originaldatei.
- `data/uploads.sqlite3`: lokale Upload-Metadaten mit Buch-, Versions- und Auftrags-ID, gespeicherte Text-/Kapitelstände in `local_extractions` und die Warteschlange `local_import_jobs`.
- `data/pending/`: temporäre Dateien während der Prüfung; nach Erfolg/Fehler werden diese entfernt.

Die lokale SQLite-Datei hält Import, Analyse und manuelle Verwaltung. **Buchstand nach Supabase übertragen** übernimmt einen fertig analysierten Stand einschließlich Kapiteltexten und Zitaten mit stabilen IDs. Ist für das Buch ein Titel-Overlay eingerichtet, wird dessen transparentes PNG zusätzlich in den privaten Supabase-Storage hochgeladen; der Buchprofil-Eintrag enthält nur den Objektpfad. Spätere Änderungen verlangen einen erneuten Klick; das Aktivieren der Verbindung überträgt nichts automatisch. Originaldateien bleiben lokal. Lokale Ablage und Supabase getrennt sichern.

Die Buchliste zeigt Importstatus und Kapitelzahl, sobald bekannt. „Datei gespeichert“ bestätigt zunächst nur die Originalablage. Die Zitatauswahl wird separat auf der Buchseite gestartet. Bei einem harten Prozessabbruch kann eine unzugeordnete temporäre/Originaldatei zurückbleiben; ein erfolgreich bestätigter Upload besitzt dagegen immer einen Metadatensatz und seine Originaldatei.

Falls der Port belegt ist, den bereits laufenden eigenen Server beenden oder `APP_PORT` in `.env` ändern. Das Startskript lässt Fehlermeldungen sichtbar stehen. Nach späteren Änderungen an den Abhängigkeiten erneut `uv sync --locked` ausführen.

## Text und Kapitel prüfen (Schritt 5)

Neue Uploads werden automatisch zum Einlesen vorgemerkt. Ältere lokale Projekte übernimmt der Worker ebenfalls: fertige Ergebnisse bleiben erhalten, noch nicht eingelesene Dateien werden eingereiht. Alternativ auf **„Text und Kapitel einlesen“** klicken. Das Ergebnis wird dauerhaft und atomar gespeichert; erneuter Upload derselben Datei bewahrt bestätigte Kapitelkorrekturen.

- Word-Überschriften (auch über abgeleitete Formatvorlagen), „Kapitel 1“, „KAPITEL: 1“, römische Nummern sowie Prolog/Epilog werden erkannt. Von Word-Gliederungsebenen wird die höchste vorkommende als Kapitelgrenze verwendet; tiefere Überschriften bleiben im Kapiteltext.
- Kapitelnummer und Titel können in einem Absatz mit Zeilenumbruch oder in zwei Absätzen stehen. Separate Titelzeilen werden bei Überschriftformat oder Großschreibung zugeordnet; unsichere Zuordnungen verlangen Prüfung.
- Word-Initialen in separaten Rahmenabsätzen werden bei eindeutigem Anschluss verbunden, etwa `D` + `er Motor …`. Initialen innerhalb desselben Absatzes bleiben durch das Zusammensetzen der Textläufe vollständig. Unklare Fälle bekommen einen Prüfhinweis.
- Leerabsätze, Kopf-/Fußzeilen und strukturell erkennbare Inhaltsverzeichnisse werden ausgelassen. Ein manuell getipptes Inhaltsverzeichnis ohne entsprechende Word-Struktur kann nicht sicher erkannt werden; die Vorschau prüfen.
- Textfelder, offene Änderungsverfolgung und weitere nicht vollständig unterstützte Inhalte verlangen eine Korrektur in Word. Bilder werden ohne Texterkennung ausgelassen. Solche Hinweise werden durch die Bestätigung von Kapitelgrenzen nicht aufgehoben.

Auf der Buchseite führt ein Kapitelklick zum eingelesenen Text. Unter **„Kapitelaufteilung prüfen und korrigieren“** lassen sich Anfangsabsätze, Titel und die Anzahl ausgelassener Überschriftabsätze (0–2) ändern. Kapitel in aufsteigender Absatzreihenfolge eintragen; der erste beginnt beim ersten erhaltenen Absatz. Die Kapitelübersicht zeigt die Startabsätze; den Text mit Absatznummern findest du auf der jeweiligen Kapitelseite. Lücken in den Nummern entstehen durch ausgelassene oder mit Initialen verbundene Absätze. Der erste Absatz eines verbundenen Paares ist seine auswählbare Fundstelle.

Ohne erkennbare Grenzen wird der gesamte Text zunächst als ein Kapitel zur Prüfung angezeigt. Text vor dem ersten Kapitel bleibt als Vorspann erhalten. **„Text eingelesen“ ist keine Promotionsfreigabe**; die KI-Analyse startet erst auf Knopfdruck.

Technische Quellzuordnung, Erkennungsgrenzen und XML-Referenz: [docs/docx-import.md](docs/docx-import.md). Die Extraktion benötigt keine zusätzlichen Pakete, keinen KI-Key und keine laufende Word-Anwendung.

## Hintergrundimport und Status (Schritt 6)

`start.bat` startet automatisch einen separaten Python-Worker. Er verarbeitet ein Buch zur Zeit. Weitere Uploads bleiben in der dauerhaften Warteschlange; die Oberfläche bleibt während des Einlesens bedienbar. Die Buchseite zeigt die Arbeitsschritte „Datei prüfen“, „Text lesen“ (mit Absatzfortschritt), „Kapitel zuordnen“ und anschließend das gespeicherte Ergebnis. Buchliste und Importseite aktualisieren sich automatisch. Ohne JavaScript die Seite manuell neu laden.

| Status | Bedeutung |
|---|---|
| Wartet | Original gespeichert, Verarbeitung vorgemerkt |
| Wird eingelesen | Worker bearbeitet dieses Buch |
| Prüfung nötig | Text vorhanden, Kapitelgrenzen oder Word-Inhalte benötigen Prüfung |
| Text eingelesen | Extraktion abgeschlossen; KI-Zitatauswahl steht noch aus |
| Einlesen fehlgeschlagen | Fehler ansehen und über „Erneut einlesen“ einen neuen Versuch starten |

Kapitel bleiben in Originalreihenfolge anklickbar. Die Übersicht zeigt Textabsätze, Zeichen und nach abgeschlossener KI-Analyse die Anzahl geeigneter Zitate.

Nach einem Worker-Abbruch startet der Webserver den Prozess erneut. Ein abgebrochener Auftrag wird nach Ablauf seines maximal 30 Sekunden gültigen Bearbeitungsanspruchs erneut übernommen. Nach drei unterbrochenen Versuchen verlangt er einen manuellen Neustart. Normale Datei-/Verarbeitungsfehler werden direkt sichtbar und nicht endlos wiederholt. Ein doppelter Upload startet einen fehlgeschlagenen Auftrag nicht erneut; dazu dient der Knopf auf der Buchseite.

Nach Schließen der gesamten Anwendung bleiben Aufträge und fertige Textstände gespeichert. Beim nächsten Start geht es weiter. Ein noch nicht gespeicherter Extraktionsversuch beginnt erneut beim Einlesen; bereits gespeicherte Ergebnisse und manuelle Korrekturen werden wiederverwendet. Die separat gestartete KI-Analyse speichert zusätzlich Zwischenstände pro Arbeitsschritt. Die Textextraktion selbst benötigt keine KI-Aufrufe. Supabase-Schreibzugriffe erfolgen ausschließlich über die gesonderte Buchübertragung.

Worker-Vertrag und Wiederaufnahme: [docs/import-worker.md](docs/import-worker.md).

## Open WebUI verbinden (Schritt 7)

1. `OPENWEBUI_URL` als HTTPS-Basisadresse ohne angehängtes `/api` und `OPENWEBUI_API_KEY` in der privaten `.env` eintragen. Einen gegebenenfalls nötigen Unterpfad der Installation beibehalten.
2. Anwendung starten und **Einstellungen → Verbindung prüfen und Modelle laden** wählen. Dabei wird nur die Modellliste abgefragt.
3. Bei Bedarf Modelle nach Name oder ID durchsuchen, ein Modell auswählen und **Modell speichern** drücken. Die Auswahl ersetzt nur `OPENWEBUI_MODEL` in der beim Start gewählten ENV-Datei. Die übrigen Einträge einschließlich Keys bleiben erhalten.
4. **Testantwort erzeugen** prüft einen kurzen technischen Austausch. Das kann beim angebundenen Modellanbieter Kosten verursachen. Buchtexte werden nicht übertragen.

Eine als Prozessvariable gesetzte `OPENWEBUI_MODEL` hat weiterhin Vorrang. In diesem Fall verweigert die Oberfläche das Speichern mit einem Hinweis; die Prozessvariable ändern und neu starten. Für individuelle Modelle müssen API-Zugriff und Textgenerierung in Open WebUI erlaubt sein.

Alternativ in PowerShell:

```powershell
# Zugang und konfiguriertes Modell prüfen, ohne Generierung:
.\.venv\Scripts\python.exe -X utf8 -m bookpromo check-openwebui

# Zusätzlich eine kurze Testantwort erzeugen:
.\.venv\Scripts\python.exe -X utf8 -m bookpromo check-openwebui --generate
```

Der Client unterstützt Textantworten und per Pydantic validierte JSON-Antworten. Er hat Zeit-/Größenlimits und begrenzte Wiederholungen bei vorübergehenden Fehlern. Ein Timeout nach möglicher Annahme der Generierung wird nicht automatisch wiederholt. Details und Fehlerbehandlung: [docs/openwebui.md](docs/openwebui.md).

Die vorhandene Verbindung wurde mit dem konfigurierten Modell erfolgreich live geprüft: Modellliste, technische Textantwort und strukturierte JSON-Antwort. **Automatische Buchanalyse und Zitatauswahl werden durch Schritt 7 noch nicht gestartet.**

## Buch analysieren (Schritt 8)

1. Buch öffnen und gegebenenfalls die Kapitelaufteilung prüfen und bestätigen. Solange blockierende Importhinweise bestehen, ist die Analyse gesperrt.
2. **KI-Analyse starten** drücken. Der Hintergrund-Worker überträgt den Text abschnittsweise an das konfigurierte Open-WebUI-Modell. Diese Aufrufe können beim Modellanbieter Kosten verursachen.
3. Fortschritt auf der Buchseite verfolgen. Zuerst entstehen Zusammenfassungen aller Kapitel und ein internes Buchprofil einschließlich Spoilern. Danach wählt die KI mit diesem Gesamtkontext Zitate aus.
4. Nach Abschluss ein Kapitel öffnen: Originalzitate, Bewertung, Begründung, Spoilerstufe und Fundstelle stehen dort. Das Buchprofil enthält außerdem Vorschläge für Bildprompt-Basis und Caption-Vorgaben.

Nur eindeutig im eingelesenen Original belegte Zitate werden gespeichert. Umformulierungen und erfundene Stellen werden abgelehnt; doppelte und stark überlappende Kandidaten werden bereinigt. Standardmäßig gelten ein Durchschnitt von mindestens 4/5 und Verständlichkeit von mindestens 4/5 als geeignet. Hohe Spoilerstufe schließt die Nutzung aus. Ein Abschluss ohne geeignete Zitate wird ausdrücklich angezeigt. Inhaltliche Qualität und Spoilereinschätzung bleiben KI-Bewertungen; die Worttreue prüft der Code.

Abgeschlossene Zwischenstände bleiben bei Abbruch erhalten. **Analyse fortsetzen** nimmt einen fehlgeschlagenen Lauf mit derselben Konfiguration wieder auf; ein identischer abgeschlossener Lauf wird wiederverwendet. Änderungen an Modell, Analyseoptionen oder Quellrevision ergeben einen neuen Lauf. Kapitelkorrekturen markieren bisherige Analysen als veraltet. Noch nicht gespeicherte Kapitelkorrekturen werden durch die automatische Statusanzeige nicht überschrieben.

Einzelne Zitatvorschläge ohne eindeutige, wortgetreue Originalfundstelle werden übersprungen. Die übrigen Vorschläge desselben Abschnitts bleiben erhalten; Gründe und Absatzangaben erscheinen nach Abschluss unter der KI-Buchanalyse. Auch Abschnitte ohne gültige Vorschläge werden gespeichert und beim Fortsetzen nicht erneut angefragt. Die Fortschrittsanzeige zeigt nur tatsächlich noch auszuführende KI-Schritte. Verbindungs- und Antwortformatfehler bleiben als Fehler sichtbar und können weiterhin eine Fortsetzung erfordern.

Die Ergebnisse liegen lokal in `local_analysis_runs` und `local_analysis_checkpoints` in `data/uploads.sqlite3`. Profile und Zitatsperren lassen sich seit Schritt 9 verwalten. Fertige Stände werden über den Supabase-Knopf auf der Buchseite übertragen. Ablauf, Limits und Wiederaufnahme: [docs/book-analysis.md](docs/book-analysis.md).

Verifiziert mit automatisierten Tests einschließlich eines echten separaten Workers sowie live mit einem synthetischen Buch aus zwei Kapiteln: fünf KI-Anfragen, vier wortgetreu belegte und als geeignet eingestufte Zitate. Ein vollständiges Nutzerbuch wurde dabei nicht analysiert.

## Buchprofile und Zitate verwalten (Schritt 9)

- **Buch → Bucheinstellungen bearbeiten:** Titel, Autor, Zieladresse, Freigabemodus, Carousel-Aktivierung sowie Buchprofil, Bildprompt-Basis und Caption-Vorgaben bearbeiten und speichern. Unter **Bild-Overlay** eine lokale Schriftart und Titelfarbe wählen; die Seite rendert eine transparente 1080×1350-Vorschau mit dem Buchtitel links oben. Die Aktivierung startet noch keine Veröffentlichung.
- **Carousel-Schlussseite:** Frontcover und Logo als PNG, JPEG oder WebP hochladen und einen CTA-Text pflegen. Python erzeugt aus dem Frontcover einen 2.5D-Buch-Mockup und daraus eine reproduzierbare 1080×1350-JPEG-Vorschau mit CTA-Text, Titel-Overlay und Logo. Die Promotion lässt sich erst aktivieren, wenn das vollständige Bild erfolgreich gerendert werden kann. Beim ausdrücklichen Buch-Sync rendert Python Overlay und Schlussseite erneut und lädt nur diese fertigen Assets digestbasiert in den privaten Supabase-Bucket; Frontcover und Logo bleiben lokal. Der aktuelle Python-Stand setzt Supabase-Schema v5 voraus.
- **Profil mit KI ausfüllen:** Die acht Profilfelder mit je einem KI-Aufruf vorschlagen lassen. Ein passender gespeicherter Buchkontext wird wiederverwendet; nur fehlender Kontext wird aus dem Buchtext vorbereitet. Über „Vorschläge in die acht Profilfelder einsetzen“ ins Formular übernehmen, prüfen und speichern. Titel, Autor, Zieladresse und Promotion-Schalter bleiben manuell; Kapitel und bestehende Zitate bleiben erhalten. Der erste Lauf benötigt zusätzliche Kontextaufrufe; Wiederholungen mit vollständigem passendem Kontext nur die acht Feldaufrufe, zuzüglich möglicher Validierungswiederholungen.
- Das erste Profil ist mit dem aktuellen KI-Vorschlag vorbelegt. Ein gespeichertes Profil bleibt bei erneuter Analyse unverändert. **Aktuellen KI-Profilvorschlag zur Bearbeitung laden** setzt einen neuen Vorschlag ins Formular; erst Speichern übernimmt ihn. Titel, Autor, Zieladresse und Promotion-Vormerkung bleiben dabei erhalten.
- **Buch → Kapitel:** Zitate sperren oder die manuelle Sperre aufheben. Originaltext und KI-Bewertung bleiben schreibgeschützt. Ein ungeeignetes Zitat wird durch Entsperren nicht nutzbar.
- Filter zeigen **Alle**, **Nutzbar**, **Gesperrt** oder **KI: nicht geeignet**, mit bis zu 20 Zitaten pro Seite. Buch- und Kapitelübersicht berücksichtigen manuelle Sperren in ihren Zitatzahlen.
- Änderungen werden lokal gespeichert und überstehen einen Neustart. Zitatsperren bleiben auch bei erneuter Analyse derselben Fundstelle erhalten. Veraltete Formulare dürfen neuere Einstellungen nicht überschreiben.

Die Verwaltung nutzt zusätzlich `local_book_settings`, `local_book_assets` und `local_quote_controls` in derselben privaten SQLite-Datei. Cover, Logo und gerenderte Carousel-Vorschauen liegen unter `data/book-assets/`. Es wird keine weitere Konfiguration benötigt. Nach dem Update eine laufende Anwendung neu starten. Details: [docs/book-management.md](docs/book-management.md).

Bei aktiver Supabase-Verbindung werden Veröffentlichungszeiten und Reservierungen für übertragene Zitate geladen. Die zusätzlichen Filter **Unbenutzt** und **Verwendet** berücksichtigen nur bestätigte Datenbankstände. Die beiden n8n-Exporte erzeugen Hero, lesbare Zitat-Slides und CTA, speichern sie temporär privat in Supabase und veröffentlichen sie als Instagram-Carousel. Review wartet auf zwei Telegram-Freigaben; Auto nutzt die in v5 protokollierten automatischen Freigaben. Beide starten inaktiv und mit `publish_enabled:false`. [Einrichtung und Testablauf](n8n/README.md).

## Einstellungen

| Variable | Bedeutung |
|---|---|
| `APP_HOST` | Lokaler Host; Standard `127.0.0.1` |
| `APP_PORT` | Lokaler Web-Port; Standard `8000` |
| `APP_DATA_DIR` | Private Laufzeitdaten; Standard `data`, relativ zur verwendeten ENV-Datei |
| `APP_MAX_UPLOAD_MB` | Maximale DOCX-Dateigröße in MB; Standard `25`, erlaubt `1` bis `100` |
| `SUPABASE_ENABLED` | Standard `false`; erst nach Schemaeinrichtung in Schritt 3.2 auf `true` setzen |
| `SUPABASE_URL` | Supabase-Projekt-URL |
| `SUPABASE_SECRET_KEY` | Nur serverseitiger Supabase-Zugang |
| `OPENWEBUI_URL` | Basis-URL der eigenen Open-WebUI-Instanz |
| `OPENWEBUI_API_KEY` | Persönlicher API-Key für die Analyse |
| `OPENWEBUI_MODEL` | Modell-ID der eigenen Instanz |
| `OPENWEBUI_TIMEOUT_SECONDS` | Gesamtzeitlimit pro HTTP-Operation einschließlich Wiederholungen; Standard `120`, erlaubt `5` bis `600` Sekunden |
| `OPENWEBUI_MAX_RETRIES` | Zusätzliche Versuche bei Verbindungsaufbau oder ausdrücklich vorübergehenden HTTP-Fehlern; Standard `2`, erlaubt `0` bis `3` |
| `ANALYSIS_CHUNK_CHARS` | Textzeichen pro Abschnitt; Standard `12000`, erlaubt `2000` bis `24000` |
| `ANALYSIS_MIN_SCORE` | Mindestdurchschnitt und Mindestverständlichkeit; Standard `4`, erlaubt `1` bis `5` |
| `ANALYSIS_MAX_QUOTES_PER_CHAPTER` | Maximale gespeicherte Zitate je Kapitel; Standard `12`, erlaubt `1` bis `50` |
| `ANALYSIS_MAX_CALLS` | Höchstzahl logischer KI-Anfragen pro Analyselauf einschließlich fachlicher Wiederholungen; Standard `1000`, erlaubt `1` bis `5000`; zusätzliche HTTP-Wiederholungen sind separat begrenzt |

Die fünf Service-Einstellungen dürfen zunächst leer bleiben. Echte Umgebungsvariablen haben Vorrang vor Einträgen in der ENV-Datei. Unbekannte Einträge in dieser Datei werden als Fehler gemeldet, damit Tippfehler auffallen. Andere Umgebungsvariablen des Betriebssystems stören nicht.

Der Check zeigt nur Vollständigkeit und Variablennamen, niemals Keys oder Konfigurationswerte. Er prüft keine Verbindungen, legt keine Datenbank an und schreibt keine Buchdaten.

```powershell
# Fehlende Supabase-Einstellungen als Fehler melden:
.\check-config.bat --require supabase

# Alle künftig benötigten Service-Einstellungen verlangen:
.\check-config.bat --require all

# Explizit eine andere lokale Datei verwenden:
.\check-config.bat --env-file .env.local

# Tests ausführen:
uv run --locked pytest

# Alternativ direkt mit der bereits eingerichteten Projektumgebung:
.\.venv\Scripts\python.exe -X utf8 -m pytest
```

Exitcode `0`: gültige Konfiguration und gegebenenfalls vollständig verlangte Felder. Exitcode `2`: ungültige/fehlende Konfiguration. Vollständige Einstellungen bestätigen noch keine gültigen Zugangsdaten.

Die eingerichtete Verbindung lässt sich mit `.\.venv\Scripts\python.exe -X utf8 -m bookpromo check-db` geprüft. Dieser Befehl verlangt die ausdrückliche Aktivierung und führt ausschließlich Lesezugriffe aus. Das SQL-Anlegen geschieht separat über den Supabase-Verwaltungszugang beziehungsweise SQL Editor.

## Git und private Dateien

- `.env` und lokale Varianten, `.venv`, `data/`, Uploads, Ausgaben, Logs, Buchdateien und lokale Datenbankdateien sind ausgeschlossen.
- Nur `.env.example` mit leeren Platzhaltern gehört ins Repository.
- Private Laufzeitdateien unter `data/` ablegen; ein eigenes Verzeichnis muss ebenfalls ausgeschlossen werden.
- OpenAI-, Telegram- und Instagram-Zugänge werden später separat in n8n eingerichtet.
- Ein lokales Repository oder GitHub-Repository wurde durch Schritt 1 nicht angelegt.

Vor einem späteren ersten Commit `git status --short` und `git diff --cached` prüfen. `.gitignore` verhindert keine ausdrücklich erzwungene Aufnahme und entfernt keine bereits versionierten Dateien.

## Projektstruktur

```text
src/bookpromo/       Python-Paket, FastAPI, Konfiguration und lokale Upload-Ablage
src/bookpromo/templates/  HTML-Seiten für Bücher und Einstellungen
src/bookpromo/static/     Lokale Styles und Verhalten bei Dateiablage
sql/                Anlegescript, Prüf-SQL und Anleitung für Schritt 3.2
supabase/migrations/ Versionierter, angewendeter Datenbankstand
n8n/                Workflow-JSON und Einrichtungsanleitung
tools/              Workflow-Generator und lokale SQL-/Workflow-Prüfungen
tests/              Tests zu Weboberfläche, Konfiguration und Git-Ausschlüssen
.env.example        Leere Konfigurationsvorlage
check-config.bat    Lokaler Windows-Konfigurationscheck
start.bat           Webserver starten und Browser öffnen
pyproject.toml      Paketdefinition und Abhängigkeiten
uv.lock             Aufgelöste Versionen und Paket-Hashes
```
