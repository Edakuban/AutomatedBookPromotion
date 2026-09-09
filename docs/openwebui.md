# Open-WebUI-Anbindung (Schritt 7)

## Schnittstelle

`OpenWebUIClient` ist ein asynchroner, von FastAPI unabhängiger HTTP-Client. Er verwendet `GET /api/models` und `POST /api/chat/completions` mit Bearer-Authentifizierung gemäß der [Open-WebUI-API-Dokumentation](https://docs.openwebui.com/reference/api-endpoints/). Die konfigurierte Basisadresse kann einen Installationsunterpfad enthalten; ein `/api`-Suffix darf nicht zusätzlich eingetragen werden.

`list_models()` liefert ausschließlich ID und Anzeigenamen. Anzeigenamen mit Zeilenumbrüchen werden für die Auswahl in eine Zeile überführt, IDs bleiben unverändert. Das Original-Metadatenpaket wird weder gespeichert noch zum Browser weitergereicht. Die geprüfte Instanz lieferte 1.649 Modelle und rund 62 MB Metadaten; deshalb ist die gesamte Modellantwort auf 128 MiB und 5.000 Einträge begrenzt. Antworten der Textgenerierung sind auf 2 MiB begrenzt. Es werden keine großen Modellpakete beim bloßen Öffnen der Einstellungsseite geladen.

`complete(system, user, max_tokens=...)` sendet getrennte System- und Nutzernachrichten, `stream: false`, ein festes Ausgabelimit, leere Werkzeuglisten und deaktivierte Websuche/Code-Ausführung. Der Client führt selbst keine Werkzeuge aus. Eigene serverseitige Modell-Pipelines und Modellvorgaben werden dadurch nicht administrativ verändert; für die spätere Buchanalyse ist ein passendes Textmodell auszuwählen.

Nur eine abgeschlossene Textantwort mit `finish_reason: stop` wird akzeptiert. Abgeschnittene, leere oder verweigerte Antworten, Tool-Aufrufe, Task-IDs und unerwartetes Streaming werden als Fehler gemeldet. Wenn eine Modellvorgabe Streaming erzwingt, diese Einstellung in Open WebUI ändern; der Client setzt nicht ungefragt Servervorgaben um.

`complete_json(system, user, ResultType)` sendet zusätzlich das Pydantic-JSON-Schema als `response_format: json_schema` und validiert die Antwort lokal mit strikten Typen. Ergebnismodelle sollen `extra='forbid'` und die fachlichen Grenzen festlegen. Ungültiges JSON, Markdown-Codeblöcke oder nicht passende Typen werden nicht automatisch repariert oder als Ergebnis gespeichert. Das konkrete Modell muss strukturierte Ausgaben unterstützen; eine Ablehnung führt nicht still zu unstrukturierten Antworten.

## Zeitlimits und Wiederholungen

Das Gesamtzeitlimit pro HTTP-Operation beträgt standardmäßig 120 Sekunden, einschließlich Übertragung, Wiederholungen und Wartezeiten. Der Verbindungsaufbau hat maximal 10 Sekunden. Das Limit ist über `OPENWEBUI_TIMEOUT_SECONDS` auf 5–600 Sekunden einstellbar. Ein Verbindungstest mit anschließender Generierung besteht aus zwei getrennten HTTP-Operationen.

`OPENWEBUI_MAX_RETRIES` erlaubt standardmäßig zwei zusätzliche Versuche (0–3 konfigurierbar). Erneut versucht werden fehlgeschlagene Verbindungsaufbauten sowie HTTP 429/502/503/504; HTTP 500 nur bei lesenden Anfragen. Der Backoff beginnt bei 0,5 Sekunden. Kurze numerische `Retry-After`-Angaben bis fünf Sekunden werden berücksichtigt. Längere/datumsbasierte Wartehinweise führen zu einer verständlichen Meldung für einen späteren Versuch.

Read-/Write-Timeouts, abgebrochene Übertragungen und unklare Generierungsantworten werden im Client nicht automatisch wiederholt, da die Verarbeitung serverseitig bereits begonnen haben könnte. Ungültige Credentials, Umleitungen und fachlich ungültige Antworten werden im Client ebenfalls nicht wiederholt. Der Analyse-Worker erlaubt bei ungültigem JSON oder nicht belegten Kandidaten einen weiteren fachlichen Versuch und speichert abgeschlossene Ergebnisse als Checkpoints. Ein expliziter Wiederholungsversuch nach einem Fehler kann den noch nicht gespeicherten Arbeitsschritt erneut beim Anbieter ausführen.

## Zugangsdaten und Modellwahl

URL und API-Key liegen ausschließlich in der privaten ENV-Konfiguration. TLS-Prüfung bleibt aktiviert; Umleitungen werden nicht verfolgt, und Proxy-Umgebungsvariablen werden nicht automatisch übernommen. Fehlermeldungen enthalten weder Response-Bodies noch Request-URLs, Header oder ungefilterte Exceptions.

Die Einstellungsseite hat drei explizite Aktionen: Verbindung/Modelle prüfen, Modell speichern und eine kurze technische Antwort erzeugen. Schreibaktionen prüfen den lokalen Ursprung; eine Prozesssperre verhindert sich überlappende UI-Prüfungen. Der Test sendet keine Buchtexte. Die Modellwahl wird gegen die aktuell verfügbare Liste geprüft und atomar ausschließlich in `OPENWEBUI_MODEL` der beim Start gewählten ENV-Datei gespeichert. IDs mit für ENV-Dateien problematischen Steuer-/Interpolationszeichen werden abgelehnt. Bestehende Keys und übrige Einträge bleiben erhalten; temporäre Dateien tragen das von Git ausgeschlossene `.env.`-Präfix. Prozessvariablen werden nicht still überschrieben.

Die Modellauswahl gilt sofort für neue Anfragen der laufenden Webanwendung und nach Neustart aus der ENV-Datei. Seit Schritt 8 verarbeitet der Worker ausdrücklich eingereihte Buchanalysen. Jeder Analyselauf hält seine Modell-ID fest; ein späterer Modellwechsel verändert einen laufenden Auftrag nicht. Zugangsdaten werden bei der Übernahme aus der ausgewählten ENV-Datei geladen. Ein geänderter Endpunkt verhindert die Fortsetzung des alten Auftrags. Details: [book-analysis.md](book-analysis.md).

## Verifikation

Die Implementierung wurde mit Mock-Transport auf Zugriffsschutz, Fehlerbereinigung, Umleitungen, begrenzte Wiederholungen, Timeouts, Antwortgrenzen, JSON-Validierung und sichere ENV-Änderungen getestet. Zusätzlich wurden die vorhandene Instanz und das bereits konfigurierte Modell live geprüft: Modellliste, exakt erwartete technische Textantwort sowie ein validiertes JSON-Objekt mit einem booleschen Testfeld. Es wurden keine Buchinhalte übertragen und keine Modelleinstellungen auf dem Server verändert.
