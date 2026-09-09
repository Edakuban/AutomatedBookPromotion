# Implementierungsplan: Automated Book Promotion

Stand: 08.09.2026. Dieser Plan beschreibt die gesamte Umsetzung. Schritte 1 bis 9 einschließlich 3.2 sind umgesetzt. Zusätzlich sind die ausdrückliche Übertragung fertiger Buchstände, Nutzungshistorie und ein importierbarer n8n-Workflow mit getrennten Telegram-Freigaben implementiert. Supabase-Schema v2 und Serverzugriff sind live geprüft. Offen sind die n8n-Credential-Zuordnung, Telegram-Freigabeperson und der kontrollierte Gesamttest. Aktueller Bedienungsstand: README.md.

### Aufteilung von Schritt 3 auf Nutzerwunsch

- **3.1 – umgesetzt:** SQL-Anlegescript, Prüf-SQL, Server-Anbindung mit Offline-Schalter, lesende Buchübersicht, lokaler Test von Schema und API-Verhalten.
- **3.2 – umgesetzt:** Zielprojekt zugeordnet, drei versionierte Migrationen angewendet, Rechte und RLS geprüft, Verbindung aktiviert und Lese-/Schreib-Smoke-Test mit Rollback durchgeführt. Details: `docs/supabase-integration.md`.

Die erste Gesamtetappe ist abgeschlossen. Die folgenden Abschnitte dokumentieren die einzelnen Umsetzungsschritte; spätere Ergänzungen sind darunter aufgeführt.

### Schritt 4: lokal umgesetzt

- Drag-and-drop und Dateiauswahl mit Fortschritt, Dateiprüfung und verständlichen Fehlern.
- Dauerhafte Originaldatei und lokales Buchprojekt mit stabilen Buch-/Versions-/Auftrags-IDs.
- Doppelte Dateien anhand des Hashs erkennen, auch bei anderem Dateinamen.
- Buchübersicht und Detailseite; noch keine Text-/Kapitelextraktion oder KI-Analyse.
- Lokale Upload-Metadaten in `data/uploads.sqlite3`; Originale in `data/originals/`.
- Keine automatische Supabase-Übertragung beim Upload. Die spätere Integrationsetappe ergänzt einen ausdrücklichen Übertragungsknopf für fertig analysierte Stände; lokale und entfernte Buchlisten bleiben getrennt.

### Schritt 5: lokal umgesetzt

- Automatisches Einlesen neuer DOCX-Uploads; manuell startbarer Import für vorhandene Uploads.
- Absatz- und Kapiteltext mit stabilen Fundstellen, Initialenverbindung, Überschriften-/Kapitelmustern und zusammengeführten Titelzeilen.
- Gespeicherte Extraktionsstände, Kapiteltextansicht und Korrekturformular für unsichere Kapitelgrenzen.
- Sichtbare Hinweise bei nicht vollständig unterstütztem Word-Inhalt; keine KI-Analyse und keine automatische Fortsetzung solcher Fälle.
- Direkte, abgesicherte WordprocessingML-Auswertung mit dem vorhandenen XML-Parser statt zusätzlichem `python-docx`: So verwenden normale Textabsätze, Initialenrahmen und Strict-OOXML dieselbe Quellzuordnung. Details: `docs/docx-import.md`.
- Lokale, atomare Speicherung in `local_extractions`; Hintergrund-Worker in Schritt 6 und Supabase-Übertragung in der Integrationsetappe ergänzt.

### Schritt 6: lokal umgesetzt

- Kapitelübersicht mit Status, Absatz-/Zeichenzahlen und Kapiteltext; Buchliste mit Importstatus und Kapitelzahl.
- Separater Python-Worker, automatisch mit der Webanwendung gestartet und beendet.
- Persistente Warteschlange, atomare Übernahme, zeitlich begrenzter Bearbeitungsanspruch und Lebenszeichen; ein aktives Buch pro Ablage.
- Automatische Fortschrittsanzeige, verständliche Fehler und manueller Wiederholungsversuch.
- Wiederaufnahme nach Abbruch, begrenzte automatische Wiederholungen und Wiederverwendung vollständiger Extraktionsstände einschließlich Kapitelkorrekturen.
- Automatische Übernahme vorhandener lokaler Projekte ohne Neuupload. Noch keine KI-Analyse, keine Zitatstände und keine Supabase-Übertragung.
- Betriebs- und Testdetails: `docs/import-worker.md`.

### Schritt 7: umgesetzt und live geprüft

- Serverseitiger Open-WebUI-Client für Modellliste, Textgenerierung und strukturierte JSON-Antworten.
- Verbindungstest, durchsuchbare Modellwahl und technische Testgenerierung in den Einstellungen; alternativ CLI `check-openwebui`.
- Modellwahl sicher in der privaten ENV-Datei speichern; URL und API-Key bleiben serverseitig.
- Zeit-/Größenlimits, begrenzte Wiederholungen und bereinigte Fehlermeldungen; keine automatische Wiederholung bei unklarem Generierungs-Timeout.
- Vorhandene Zugangsdaten mit der vom Nutzer genannten Domain geprüft: Modellliste, kurze Textantwort und validierte JSON-Antwort erfolgreich. Keine Buchtexte übertragen.
- Buchkontext, Zitatauswahl und die Anbindung dieser Analyse an den Worker folgen in den nächsten Schritten. Details: `docs/openwebui.md`.

### Schritt 8: umgesetzt und live geprüft

- Expliziter Analysestart auf der Buchseite; blockierende Importhinweise verhindern KI-Aufrufe.
- Abschnittsweise Kapitelzusammenfassungen, mehrstufige Verdichtung langer Bücher und internes Buchprofil vor der Zitatauswahl.
- Vorschläge für Bildprompt-Basis und Caption-Vorgaben; Spoilerkontext aus dem gesamten Buch.
- Strukturierte Kandidaten mit Bewertung und Begründung; eindeutige wortgetreue Quellprüfung, Duplikat-/Überlappungsfilter und Ausschluss hoher Spoilerstufe von der Nutzung.
- Dauerhafte Aufträge und Zwischenstände; begrenzte Wiederholungen, gespeichertes Anfragebudget und Wiederaufnahme nach Abbruch im vorhandenen Worker.
- Buchprofil und geeignete Zitatzahlen auf der Buchseite; Zitate mit Bewertung, Kontext und Fundstellen auf Kapitelseiten. Abschluss ohne geeignete Zitate wird sichtbar ausgewiesen.
- Neue Quellrevision macht alte Ergebnisse ungültig. Keine automatische Supabase-Übertragung und keine Veröffentlichung.
- 143 automatisierte Tests erfolgreich; Live-Test mit synthetischem Zweikapitelbuch: fünf KI-Anfragen, vier belegte geeignete Zitate. Details: `docs/book-analysis.md`.

### Schritt 9: lokal umgesetzt

- Bucheinstellungen für Titel, Autor, Zieladresse, Promotion-Vormerkung, internes Profil, Bildprompt-Basis und Caption-Vorgaben.
- Eigene Speicherung manueller Änderungen; neue KI-Analysen überschreiben sie nicht. Aktuelle KI-Profilvorschläge können ausdrücklich zur Bearbeitung geladen und gespeichert werden.
- Zitate sperren/entsperren, ohne Originaltext oder KI-Bewertung zu verändern. Hohe Spoilerstufe und ungeeignete Bewertungen bleiben Ausschlussgründe.
- Filter für alle, nutzbare, gesperrte und als ungeeignet bewertete Zitate; Seitennavigation und aktuelle Zähler auf Buch-/Kapitelseiten.
- Sperren an Wortlaut und Originalabsätze der Buchversion gebunden, damit sie bei erneuter Analyse derselben Stelle erhalten bleiben. Revisionsprüfungen schützen vor überschreibenden alten Formularen.
- Lokale Daten in `local_book_settings` und `local_quote_controls`; Veröffentlichungshistorie, Nutzungsfilter und tatsächliche Promotion-Auswahl sind durch die anschließende n8n-/Datenbankintegration ergänzt.
- Technische und Bedienungsdetails: `docs/book-management.md`.

### Ergänzung vom 08.09.2026: vorhandener n8n-Flow

Der bereitgestellte Feiertagsflow dient als Referenz. Gewünschter Ablauf: erst den vollständigen Text in Telegram freigeben, dann das Bild erstellen und freigeben, anschließend veröffentlichen. Wiederverwendbare Bausteine und notwendige Anpassungen sind in `docs/n8n-reference-flow.md` dokumentiert. Der vorhandene Flow wurde nur gelesen, weder verändert noch ausgeführt.

### Integrationsetappe: umgesetzt, Gesamttest noch offen

- Supabase-Schema v2 mit atomarer Buchübertragung, Reservierung und getrennten Freigabeübergängen eingerichtet.
- Fertige Buchstände einschließlich manueller Profile und Zitatsperren ausdrücklich übertragen; wiederholte identische Übernahme bleibt ohne Duplikate.
- Letzte bestätigte Veröffentlichung und Reservierung auf Kapitelseiten, Filter für unbenutzte und verwendete Zitate.
- `n8n/book-promotion.json`: inaktiver Export für n8n 2.35.4, Tagesstart um 06:00 Uhr Europe/Berlin, Textfreigabe vor Bildgenerierung, Bildfreigabe vor Veröffentlichung, begrenzte Wiederholungen und manuelle Vorschaufortsetzung.
- Lokale Python-, PostgreSQL- und Workflow-Prüfungen bestanden; Supabase-Live-Test ohne bleibende Testbücher.
- Übergabe als JSON auf Nutzerwunsch. Credentials und erlaubte Telegram-Benutzer-ID einrichten, erstes Buch analysieren/übertragen und Ablauf kontrolliert testen. Veröffentlichungen sind im Export zunächst gesperrt. Token-Erneuerung und direkte access_token-Übergabe sind aus dem Feiertagsflow übernommen. Dauerhafte Medienablage und externer Abgleich unklarer Veröffentlichungen bleiben offen. Anleitung: `n8n/README.md`.

## 1. Ziel und vereinbarter Umfang

Eine schlichte Python-Webanwendung importiert eigene Bücher aus DOCX, lässt eine KI geeignete Originalzitate auswählen und verwaltet Bücher, Kapitel, Zitate und Buchprofile in Supabase. n8n verwendet dieselben Daten für die tägliche Produktion und Veröffentlichung von Instagram-Posts nach Freigabe über Telegram.

Die Oberfläche ist textorientiert: Tabellen, Formulare, Statusanzeigen und eine klare Navigation. Bildgenerierung, Post-Vorschauen und Veröffentlichungsaktionen liegen in n8n beziehungsweise Telegram.

### Navigation

1. **Buchübersicht:** DOCX per Drag-and-drop oder Dateiauswahl hinzufügen, neues Buchprojekt anlegen, Analysefortschritt und Verfügbarkeit anzeigen.
2. **Buch öffnen:** Kapitel in Originalreihenfolge mit Analysezustand und Anzahl nutzbarer Zitate anzeigen.
3. **Kapitel öffnen:** Zitate mit Originaltext, KI-Bewertung, Auswahlbegründung, Nutzbarkeit und letztem erfolgreichen Instagram-Post anzeigen. Ohne Veröffentlichung: „Noch nie veröffentlicht“.
4. **Bucheinstellungen:** Titel, Autor, Buchprofil, Bildprompt-Basis, Caption-Vorgaben, Ziel-URL und Teilnahme an der Promotion bearbeiten.
5. **Promotion-Einstellungen:** Festes Buch oder zufälliges aktives Buch auswählen.

Die KI übernimmt die Zitatauswahl automatisch. Einzelne Zitate können nachträglich gesperrt werden; eine manuelle Einzelabnahme aller Zitate ist nicht Voraussetzung für den Betrieb.

### Täglicher Ablauf

- n8n startet um **06:00 Uhr in Europe/Berlin**, einschließlich Sommer-/Winterzeitwechsel.
- Ein vorhandener offener Post für das konfigurierte Instagram-Konto verhindert einen weiteren Tagesentwurf.
- Buch und Zitat auswählen, vollständigen Begleittext mit Originalzitat erstellen und zur Textprüfung an Telegram senden.
- Textphase: **Text freigeben**, **Text neu erstellen**, **Verwerfen**. Erst die Textfreigabe startet die Bildgenerierung.
- Bildphase: Bild zusammen mit dem unveränderten freigegebenen Text zeigen; **Bild freigeben und posten**, **Bild neu erstellen**, **Text neu erstellen**, **Beides neu erstellen**, **Verwerfen**.
- Neuerstellen behält das Originalzitat bei und erzeugt eine neue Vorschauversion. Textänderungen verlangen erneut Textfreigabe; „Bild neu“ behält den freigegebenen Text. Nach einer Textänderung wird erst nach deren Freigabe ein dazu passendes neues Bild erzeugt.
- Veröffentlichung erfolgt erst nach Freigabe, nicht zwingend um 06:00 Uhr.
- Verwerfen beendet den Entwurf ohne Veröffentlichung. Der nächste reguläre Lauf kann wieder einen Entwurf erstellen.

## 2. Technischer Aufbau

| Baustein | Entscheidung |
|---|---|
| Anwendung | Python mit FastAPI |
| Oberfläche | Jinja2-HTML-Vorlagen, einfaches CSS, wenig JavaScript für Upload und Statusabfragen |
| DOCX-Import | Direkte WordprocessingML-Auswertung mit defusedxml für Absätze, Initialen und stabile Quellpositionen |
| KI-Analyse | HTTP-Aufrufe an die eigene Open-WebUI-Instanz; Modell konfigurierbar |
| Validierung | Strukturierte Antwortmodelle, beispielsweise mit Pydantic; Originaltextprüfung im Python-Code |
| Gemeinsame Daten | Supabase Postgres |
| Importverarbeitung | Separater Python-Worker mit persistenten Aufträgen und Checkpoints |
| Tägliche Produktion | n8n mit OpenAI-Bildgenerierung und Open WebUI für Begleittext/Bildprompt |
| Freigabe | Telegram-Bot mit versionierten Entscheidungen |
| Medien | Von n8n verwalteter Speicher mit für Instagram abrufbaren URLs; konkreten Speicher vor Integration festlegen |

### Betrieb

Die erste Version läuft lokal unter Windows. Ein Startskript startet Webserver und Import-Worker; die Oberfläche wird im Browser geöffnet. Beim Schließen des Browsers läuft die Analyse weiter, solange die Prozesse und der Rechner laufen. Nach einem Prozessabbruch wird der letzte gespeicherte Arbeitsstand wieder aufgenommen.

Der Worker bearbeitet zunächst ein Buch zur Zeit. Aufträge werden atomar übernommen, besitzen einen zeitlich begrenzten Bearbeitungsanspruch und melden regelmäßig ihren Zustand. Ein abgestürzter Worker darf einen Auftrag dadurch nicht dauerhaft blockieren. Ein zusätzlicher Message-Broker ist für die erste Version nicht vorgesehen.

Original-DOCX-Dateien liegen zunächst lokal in einem von Git ausgeschlossenen Datenverzeichnis. Der für Analyse und Fundstellen benötigte extrahierte Text sowie die fachlichen Ergebnisse werden in Supabase gespeichert. n8n benötigt keinen Zugriff auf die Word-Dateien. Ein späterer Serverumzug muss lokale Originaldateien bei Bedarf mitnehmen.

FastAPI, Worker und eine mögliche spätere CLI verwenden dieselbe Python-Importlogik. Eine zusätzliche vollständige CLI-Oberfläche gehört nicht zum ersten Lieferumfang.

## 3. Datenmodell

Tabellen und SQL-Funktionen werden durch versionierte Migrationen angelegt. IDs sind stabile technische IDs; Kapitelreihenfolge und Fundstellen werden separat gespeichert. Zeitpunkte werden als UTC-Zeitstempel gespeichert und in der Oberfläche in Europe/Berlin dargestellt.

| Tabelle | Zentrale Inhalte |
|---|---|
| `books` | ID, Titel, Autor, Beschreibung, aktiv für Promotion, Bildprompt-Basis, Caption-Vorgaben, Welt-/Figurenbeschreibung, Ziel-URL, freigegebene aktuelle Buchversion |
| `book_versions` | Buch-ID, Dateiname, Datei-Hash, Importzustand, Importzeit, interne Gesamtzusammenfassung, Analyse-/Promptversion |
| `chapters` | Buchversion-ID, Reihenfolge, Kapitelname, extrahierter Originaltext, Absatzstruktur, interne Zusammenfassung, Analysezustand |
| `quotes` | Kapitel-ID, Originaltext, genaue Quellpositionen, Kontext, Kriterienbewertungen, Auswahlbegründung, Spoilereinstufung, nutzbar/gesperrt |
| `import_jobs` | Buchversion-ID, Zustand, aktueller Schritt/Kapitel, Fortschritt, Versuche, Checkpoints, Worker-Anspruch, letzter Lebensnachweis, Fehlermeldung |
| `posts` | Zitat-ID, Zielkonto, Zustand, Vorschauversion, festgehaltenes Originalzitat/Buchprofil, Bildprompt, Caption, Medienpfad, Telegram-Nachricht, Freigabe, Instagram-Container-/Medien-ID, Versuche, Fehler, Zeitpunkte |
| `promotion_settings` | Konfigurations-ID, aktiv, Modus `fixed_book`/`random_book`, festes Buch, Zielkonto-Verweis, Wiederverwendungsregel |

API-Keys und Tokens werden nicht in diesen fachlichen Tabellen gespeichert. `account_id` ist ein Verweis auf das konfigurierte Zielkonto; n8n verwaltet die zugehörigen Credentials.

### Integritätsregeln

- Fremdschlüssel verbinden Bücher, Versionen, Kapitel, Zitate und Posts.
- Identische Datei innerhalb desselben Buchs nicht erneut importieren.
- Identische Zitatfundstelle innerhalb eines Kapitels nicht mehrfach speichern; inhaltlich stark überlappende Kandidaten vor Auswahl zusammenfassen beziehungsweise reduzieren.
- Pro Zielkonto höchstens ein offener Produktions-/Freigabe-/Veröffentlichungsvorgang.
- Letzte Verwendung eines Zitats wird aus erfolgreich veröffentlichten Posts ermittelt. Erstellung oder Telegram-Versand zählen nicht als Nutzung.
- Aktive Post-Reservierungen verhindern konkurrierende Auswahl desselben Zitats.
- Vorhandene Posts und deren Originalzitate bleiben bei einem neuen Buchimport nachvollziehbar.
- Im ersten Umfang Bücher und Zitate deaktivieren statt Veröffentlichungshistorie zu löschen.

### Überarbeitete Bücher

Die Oberfläche bietet bei einem bestehenden Buch „Neue Word-Version importieren“. Eine neue Version wird getrennt analysiert. Die bisherige Version bleibt verwendbar, bis die neue erfolgreich bereitsteht. Der Wechsel beeinflusst künftige Auswahlen; bereits offene Posts behalten ihren gespeicherten Inhalt. Alte Zitate und Posts bleiben für die Historie erhalten.

## 4. Import und KI-Auswahl

### Schritt A: Datei und Buchprojekt

- DOCX-Typ und Dateigröße prüfen; beschädigte beziehungsweise nicht lesbare Dokumente verständlich melden.
- Datei sichern, Hash ermitteln, Buchprojekt oder Buchversion anlegen und Auftrag einreihen.
- Dateiname als vorläufigen Titel verwenden; Titel und Autor bleiben bearbeitbar.
- Doppelte Uploads erkennen und zum vorhandenen Import führen.

### Schritt B: Text und Kapitel extrahieren

- Absätze in Dokumentreihenfolge lesen und stabile Fundstellen vergeben.
- Word-Überschriften bevorzugt für Kapitel verwenden; zusätzlich Muster wie „Kapitel 1“ erkennen.
- Kapitelnummer und direkt folgende Titelzeile bei Bedarf zu einem Kapitel zusammenführen.
- Leerabsätze, Kopf-/Fußzeilen und Inhaltsverzeichnisse nicht als Zitatmaterial behandeln.
- Initialen anhand der Dokumentstruktur mit dem folgenden Wort verbinden. Beispiel: `D` + `er Motor läuft …` ergibt `Der Motor läuft …`.
- Textreihenfolge und auffällige Sonderfälle prüfen. Nicht unterstützte Inhalte wie bestimmte Textfelder oder offene Änderungsverfolgung sichtbar melden, statt Vollständigkeit vorzutäuschen.
- Wenn Kapitelgrenzen nicht zuverlässig erkannt werden: Import anhalten und eine einfache Korrektur der erkannten Kapitelgrenzen anbieten. Keine manuelle Zitatauswahl verlangen.

### Schritt C: Buchkontext erstellen

- Kapitel zusammenfassen; sehr lange Kapitel in begrenzte Abschnitte teilen.
- Aus den Kapitelzusammenfassungen eine interne Buchzusammenfassung einschließlich Ende und Wendepunkten erstellen.
- Daraus ein erstes Buchprofil mit Genre, Welt, Stimmung, Figurenmerkmalen und Bildstil-Vorschlag ableiten.
- Kontext zur Spoilerprüfung intern halten; nicht unverändert als öffentliche Caption verwenden.
- Manuell bearbeitete Buchprofile bei späteren Analysen nicht still überschreiben.

### Schritt D: Kandidaten auswählen und bewerten

- Pro Kapitel/Teilabschnitt zusammenhängende Originalstellen auswählen lassen. Ein bis drei Sätze sind eine Orientierung; Verständlichkeit entscheidet.
- Kriterien: Verständlichkeit ohne Vorwissen, Neugier, emotionale Wirkung, Bildpotenzial und Spoilergefahr.
- KI gibt strukturierte Ergebnisse mit Quellreferenz, Kandidatentext, Einzelbewertungen und kurzer Begründung zurück.
- Buchtext wird ausschließlich als zu analysierender Inhalt behandelt; darin vorkommende Anweisungen steuern weder das Tool noch externe Aktionen.
- Bewertungsskala, Mindestanforderungen und maximale Auswahl pro Kapitel sind konfigurierbar und werden an einem Beispielbuch abgestimmt.
- Thematische Vielfalt und ausreichenden Abstand zwischen stark ähnlichen Stellen berücksichtigen.

### Schritt E: Technisch prüfen und speichern

- Jeder Kandidat muss auf eine zusammenhängende Stelle im extrahierten Original zurückgeführt werden können.
- Der gespeicherte Zitattext wird aus dieser Quelle übernommen, nicht aus einer möglichen KI-Umformulierung.
- Technische Normalisierung, etwa von Zeilenumbrüchen, benötigt eine nachvollziehbare Zuordnung zum Original. Satzzeichen und Wörter bleiben erhalten.
- Ungültige Antworten oder nicht belegbare Zitate begrenzt erneut anfordern; danach als Fehler dokumentieren und nicht veröffentlichbar speichern.
- Checkpoint nach abgeschlossenen Schritten/Kapiteln speichern. Bei Wiederaufnahme fertige Analysen wiederverwenden.
- Geeignete, geprüfte Kandidaten automatisch nutzbar machen; gesperrte, ungeprüfte und zu spoilerreiche Kandidaten ausschließen.
- Eine vollständig analysierte Version mit null geeigneten Zitaten als solchen Zustand anzeigen, statt einen technischen Erfolg mit Promotionsbereitschaft gleichzusetzen.

## 5. Oberfläche

### Buchübersicht

- Uploadfläche mit alternativem Dateiauswahlknopf.
- Buchliste mit Titel, Status, Kapitelzahl, nutzbaren Zitaten und Promotion-Schalter.
- Zustände: wartet, wird analysiert, Eingriff erforderlich, bereit, fehlgeschlagen.
- Nach Upload direkt zum Buch mit Fortschritt navigieren.

### Kapitelübersicht

- Kapitelname und Reihenfolge, Verarbeitungszustand, Zahl nutzbarer/gesperrter Zitate.
- Fortschritt als konkrete Angabe, etwa „Kapitel 8 von 24: Zitate prüfen“.
- Fehlerdetails ohne Credentials; unterbrochene/fehlgeschlagene Arbeit gezielt wiederaufnehmen.

### Zitatübersicht

- Originalzitat, Fundstelle, KI-Bewertung, Begründung und Spoilereinstufung.
- Letzter erfolgreicher Post; bei Bedarf Link zum Instagram-Post, sofern gespeichert.
- Zusätzlicher Hinweis, wenn ein Zitat aktuell für einen Post reserviert ist.
- Nutzbar/gesperrt umschalten, nach unbenutzt/verwendet/gesperrt filtern.
- Originaltext schreibgeschützt lassen; erneute Analyse ist eine gesonderte Aktion.

### Einstellungen

- Buchprofil und Bildprompt-Basis als einfache Textfelder.
- Promotion-Modus und festes Buch auswählen.
- Verbindungstest und Modellwahl für Open WebUI; Schlüssel bleiben serverseitige Konfiguration.
- Keine Bildergalerie, keine Bildgenerierung und keine konkurrierende Freigabe in der Weboberfläche.

## 6. Schnittstelle für n8n

Vor Aufbau des Workflows wird ein kurzer Schnittstellenvertrag mit Feldnamen, Beispielantworten, Zustandsübergängen und Fehlerfällen dokumentiert.

### Auswahl

- `fixed_book`: ausschließlich das konfigurierte, aktive und bereite Buch berücksichtigen.
- `random_book`: gleichverteilt ein geeignetes aktives Buch auswählen; anschließend ein geeignetes Zitat. Bücher ohne verfügbare Zitate nehmen nicht an der Auswahl teil.
- Zunächst unbenutzte Zitate bevorzugen. Wiederverwendung standardmäßig deaktiviert; optional erst nach konfigurierbarer Sperrfrist.
- Auswahl und Anlage des reservierenden Posts in einer Datenbanktransaktion ausführen.
- Einen stabilen Lauf-Schlüssel je Zielkonto und lokalem Kalendertag verwenden, damit doppelte Cron-Ausführungen keinen zweiten Entwurf anlegen.
- Kein geeignetes Zitat: ohne Generierungskosten beenden und einen handlungsrelevanten Hinweis ausgeben.

### Post-Zustände

`generating_text → awaiting_text_approval → generating_image → awaiting_image_approval → approved → publishing → published`

Dieser Ablauf ersetzt für die spätere Workflow-Implementierung die bisher geplante gemeinsame Text-/Bildfreigabe. Das vorbereitete SQL-Schema und sein Vertrag müssen in der Integrationsetappe entsprechend angepasst und getestet werden; Schritt 9 ändert das SQL noch nicht.

Ergänzende Zustände: `regenerating`, `discarded`, `failed`, `publish_uncertain`.

- Offene Zustände inklusive `publish_uncertain` blockieren einen neuen Tagesentwurf für dasselbe Konto.
- Fehler haben eine begrenzte Wiederholungszahl; ein endgültig fehlgeschlagener Vorgang wird sichtbar beendet und seine Reservierung freigegeben, sofern keine Veröffentlichung unklar ist.
- Neuerstellen erhöht die Vorschauversion und verwirft die vorherige Freigabemöglichkeit.
- Bereits erfolgreich generierte Medien und Texte bei Fehlerwiederholung wiederverwenden, sofern keine Neuerstellung angefordert wurde.

### Telegram-Freigabe

- Nur Entscheidungen aus dem konfigurierten Chat und von erlaubten Benutzer-IDs annehmen.
- Callback referenziert Post und Vorschauversion; Statuswechsel erfolgt atomar.
- Doppelklicks, wiederholte Telegram-Updates und Buttons alter Vorschauen bleiben wirkungslos.
- Alte Vorschauaktionen nach Möglichkeit deaktivieren; unabhängig davon serverseitig validieren.
- Textfreigabe startet nur die Bildgenerierung. Die anschließende Bildfreigabe veröffentlicht exakt den bereits freigegebenen Text und das dazu angezeigte Bild.
- Verwerfen kennzeichnet das Zitat nicht als verwendet. Es wird kurzfristig von der Auswahl ausgeschlossen, damit am nächsten Morgen nicht unmittelbar dieselbe verworfene Stelle erscheint.

### Instagram

- Vor der Gesamtintegration einen einzelnen Testpost über den gewählten Meta-Login-/Token-Weg prüfen; Veröffentlichung nur mit ausdrücklich freigegebenem Testinhalt.
- Bildformat und öffentlich abrufbare Medien-URL vor Veröffentlichung validieren.
- Container-ID früh speichern; Medien-ID und Veröffentlichungszeit nach bestätigtem Erfolg speichern.
- Bei Timeout mit möglicherweise erfolgreicher Veröffentlichung zuerst externen Status abgleichen. Kein blindes erneutes Posten.
- Wenn der Erfolg nicht zuverlässig feststellbar ist: `publish_uncertain`, Hinweis über Telegram und kein automatischer zweiter Veröffentlichungsversuch.

## 7. Konfiguration und Git-Schutz

- `.gitignore` vor Eintragen erster Zugangsdaten anlegen.
- `.env`, lokale Varianten, Uploads, Laufzeitdaten, Logs, Datenbankexporte und generierte Medien ausschließen; `.env.example` ausdrücklich zulassen.
- `.env.example` enthält nur Variablennamen und ungefährliche Platzhalter.
- Importtool benötigt Supabase-URL, einen passenden serverseitigen Zugang, Open-WebUI-URL, API-Key und Modell-ID.
- n8n-Credentials enthalten separat die Zugänge für Supabase, Open WebUI, OpenAI, Telegram, Instagram und gegebenenfalls Medien-Storage.
- Exportierte n8n-Workflows dürfen keine Tokens oder fest eingebetteten Geheimnisse enthalten.
- Keine Secrets in HTML, Browser-JavaScript, Fehlermeldungen oder Logs ausgeben.
- Supabase-Tabellen in exponierten Schemas mit RLS und passenden Berechtigungen absichern. Keine anonymen Lesezugriffe auf Buchtexte einrichten; technische Schreibrechte auf den nötigen Umfang begrenzen.
- Lokal zunächst nur auf `127.0.0.1` lauschen. Vor späterem Serverbetrieb Zugangsschutz und HTTPS einrichten.
- Abhängigkeiten versionieren und Lockdatei einchecken. Vor dem ersten Commit vorgemerkte Dateien auf Buchinhalte und Secrets prüfen.

## 8. Umsetzung in Etappen

### Etappe 1: Projektbasis und Datenbankvertrag

- Python-Projekt, Abhängigkeiten, Konfigurationsmodell, `.gitignore`, `.env.example` und Startskript vorbereiten.
- FastAPI-Grundgerüst und einfache Navigation erstellen.
- Schema, Migrationen, RLS und erforderliche Transaktionsfunktionen vorbereiten.
- Zunächst isolierte Entwicklungsdatenbank verwenden; Zielprojekt erst nach eindeutiger Zuordnung verändern.

**Fertig, wenn:** Die Anwendung lokal startet, Konfiguration verständlich geprüft wird und das Schema inklusive Zugriffsregeln und Integritätsbedingungen validiert ist.

### Etappe 2: DOCX-Import und Kapitelansicht

- Upload, Buchversionen, Dateiablage, Extraktion, Initialenbehandlung und Kapitelzuordnung implementieren.
- Persistente Aufträge mit Fortschritt und Wiederaufnahme einführen.
- Buch- und Kapitelübersichten an echte Importdaten anschließen.

**Fertig, wenn:** Ein repräsentatives DOCX korrekt extrahiert wird, insbesondere Initialen und mehrzeilige Kapitelüberschriften; ein erneuter Upload erzeugt keine Duplikate.

### Etappe 3: KI-Analyse und Zitate

- Open-WebUI-Anbindung mit Modellwahl, Zeitlimits und begrenzten Wiederholungen implementieren.
- Kapitel-/Buchzusammenfassung, Buchprofil-Vorschlag, Kandidatenauswahl und Bewertung erstellen.
- Quellenprüfung, Deduplizierung und Zitatspeicherung umsetzen.
- Zitatübersicht und Bucheinstellungen fertigstellen.

**Fertig, wenn:** Ein Buch automatisch verwertbare, wortgetreue und begründete Zitate liefert; erfundene/umformulierte Kandidaten werden ausgeschlossen und Analyseabbrüche können fortgesetzt werden.

### Etappe 4: Promotion-Daten und n8n-Anbindung

- Promotion-Einstellungen, Auswahlfunktionen, Reservierung und Post-Zustände umsetzen.
- Schnittstellenvertrag und n8n-Workflow für Auswahl, Textfreigabe vor der Bildgenerierung, anschließende Bildfreigabe und Speicherung erstellen; Referenz: `docs/n8n-reference-flow.md`.
- Letzte Veröffentlichung und Reservierungsstatus in der Zitatansicht anzeigen.

**Fertig, wenn:** Beide Buchmodi funktionieren, parallele Läufe keinen zweiten offenen Post erzeugen und der Workflow mit einem gespeicherten Entwurf wiederaufgenommen werden kann.

### Etappe 5: Telegram und Instagram

- Getrennte Text-/Bildvorschauen und die jeweiligen Telegram-Aktionen implementieren.
- Versionierte, gegen doppelte Verarbeitung geschützte Freigabe anschließen.
- Instagram-Veröffentlichung einschließlich Prüfung unklarer Ergebnisse umsetzen.
- Zeitplan auf 06:00 Uhr Europe/Berlin konfigurieren; zunächst deaktiviert exportieren.

**Fertig, wenn:** Ein freigegebener Entwurf genau in der bestätigten Fassung veröffentlicht wird, alte Buttons nichts bewirken, keine Veröffentlichung vor Freigabe stattfindet und die Zitatansicht den erfolgreichen Post anzeigt.

### Etappe 6: Gesamtabnahme und Betrieb

- Ein echtes Buch komplett importieren und Ergebnisse stichprobenartig auf Texttreue, Spoiler und Eignung prüfen.
- Verbindungsfehler, Worker-Neustart, doppelte Ausführung, Neuerstellen und Verwerfen durchspielen.
- Kurze Bedienungsanleitung, Einrichtung der Credentials und Wiederaufnahme nach Fehlern dokumentieren.
- Workflow nach erfolgreichem kontrolliertem Test und Bereitstellung der Zugänge aktivieren.

**Fertig, wenn:** Upload bis freigegebenem Instagram-Post nachvollziehbar funktioniert, alle notwendigen Zustände gespeichert sind und der tägliche Betrieb eingerichtet ist.

## 9. Gezielte Tests

- DOCX mit Initiale: `D` und `er` ergeben genau `Der`; keine fehlenden oder doppelten Buchstaben.
- Kapitel mit Word-Überschrift, manueller Formatierung und Titel in Folgezeile.
- Kandidat enthält erfundene Wörter, Auslassungen oder umformulierte Satzzeichen: keine ungeprüfte Übernahme als Originalzitat.
- Zu lange Kapitel und ungültige KI-Antworten: begrenzte Verarbeitung und verständlicher Fehlerzustand.
- Importabbruch und Neustart: bereits abgeschlossene Kapitel werden nicht unnötig erneut analysiert.
- Identischer Import und neue Buchversion: keine doppelten Zitate, Historie bleibt erhalten.
- Zufallsmodus: Auswahl auf Buchebene; Bücher ohne verfügbare Zitate werden ausgeschlossen.
- Gleichzeitige Cron-Läufe, Doppelklick und veralteter Telegram-Callback: nur erlaubte Zustandsübergänge.
- Neuerstellung nur des Bilds beziehungsweise Texts: jeweils anderer Bestandteil und Zitat bleiben unverändert.
- Instagram-Timeout nach möglichem Erfolg: Abgleich oder sichtbare Unklarheit statt Doppelpost.
- Anzeige „letzter Post“ ändert sich nur bei bestätigter Veröffentlichung.
- Kein Zugriff auf private Buchdaten ohne den vorgesehenen technischen Zugang.
- Manueller Browsercheck für Drag-and-drop, Dateiauswahl, Navigation, längere Zitate und lesbare Fehlermeldungen.

## 10. Später benötigte Angaben und Dateien

Diese Punkte blockieren die Projektbasis nicht, werden aber vor den jeweiligen Integrationstests benötigt:

- Ein repräsentatives DOCX mit Initialen und typischen Kapitelüberschriften.
- Supabase-Zielprojekt und serverseitiger Zugang für das Tool; gesonderte n8n-Credentials.
- Open-WebUI-URL, API-Key und ein geeignetes verfügbares Textmodell.
- OpenAI-Zugang für n8n und gewünschtes Ausgabenlimit für Generierung/Wiederholungen.
- Telegram-Bot, Ziel-Chat und freigabeberechtigte Benutzer-ID.
- Instagram-Konto-ID, passender Token und zugehörige Meta-Konfiguration.
- Gewählter Medien-Speicher für von Instagram abrufbare Bilder.

## 11. Bewusst später

- PDF-Import, da DOCX vorhanden ist.
- Bildvorschauen oder Bildbearbeitung in der Weboberfläche.
- Desktop-Verpackung mit Tauri oder Installer.
- Mehrbenutzerverwaltung und mehrere gleichzeitig betriebene Instagram-Konten in der Oberfläche.
- Automatische Erfolgsauswertung, datenbasierte Zitatgewichtung und zusätzliche Plattformen.
- Aufwendige visuelle Gestaltung oder ein separates React-Frontend.

## 12. Erwartete Liefergegenstände

- Lokal startbare Python-/FastAPI-Anwendung mit Import-Worker.
- Einfache Oberfläche für Bücher, Kapitel, Zitate und Einstellungen.
- Versionierte Supabase-Migrationen und dokumentierte n8n-Schnittstelle.
- Importierbare n8n-Workflows ohne Secrets.
- `.env.example`, `.gitignore`, Startskript, gezielte Tests und kurze Betriebsanleitung.

## Technische Referenzen

- [FastAPI: Templates](https://fastapi.tiangolo.com/advanced/templates/)
- [FastAPI: Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/) – ersetzt für dieses Projekt nicht die eigene persistente Auftragsverwaltung.
- [python-docx](https://python-docx.readthedocs.io/en/latest/)
- [Open WebUI: API-Endpunkte](https://docs.openwebui.com/reference/api-endpoints/)
- [OpenAI: Bildgenerierung](https://developers.openai.com/api/docs/guides/image-generation)
- [Meta: Instagram API](https://www.postman.com/meta/instagram/collection/6yqw8pt/instagram-api)

Konkrete API-Felder, Modellverfügbarkeit und Berechtigungen werden vor der jeweiligen Implementierung anhand der dann aktuellen offiziellen Dokumentation und der vorhandenen Serverversionen geprüft.
