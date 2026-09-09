# Lokaler Import-Worker (Schritt 6)

## Prozess und Queue

FastAPI startet über seinen Lifespan einen separaten Python-Prozess mit dem `spawn`-Verfahren, auch unter Windows. Der Worker erhält Datenverzeichnis, Uploadlimit, eine lokale Kontrollverbindung und seit Schritt 8 den Pfad der ausgewählten ENV-Datei. Zugangsdaten werden nicht als Prozessargumente übergeben. Die Textextraktion benötigt keine externen Dienste. Erst für einen ausdrücklich eingereihten KI-Auftrag liest der Worker die Konfiguration und verbindet sich mit Open WebUI. Die Oberfläche überwacht den Prozess und startet ihn nach unerwartetem Ende erneut. Beim regulären Beenden wird zunächst ein Stoppsignal gesendet; nach drei Sekunden wird ein noch arbeitender Worker beendet. Die Kontrolle verwendet eine Pipe, deren Zustand nach einem erzwungenen Prozessabbruch keine gemeinsam genutzte Event-Sperre blockieren kann.

`local_import_jobs` entsteht automatisch in der bestehenden privaten `uploads.sqlite3`, sobald Bücher vorhanden sind. Ohne Bücher werden beim bloßen Start und bei Lesezugriffen keine Laufzeitverzeichnisse angelegt. Pro Buch existiert genau ein Auftrag mit der bereits beim Upload vergebenen `job_id`. Wartende Aufträge bleiben auch nach Server-/Rechnerneustart bestehen.

`reconcile` ergänzt fehlende Aufträge, beispielsweise nach einem Abbruch zwischen Originalspeicherung und Einreihung. Fertige Ergebnisse aus Schritt 5 werden als abgeschlossener bzw. prüfbedürftiger Auftrag übernommen. Alte fehlgeschlagene Extraktionen bleiben sichtbar fehlgeschlagen. Ein expliziter Wiederholungsversuch setzt einen fehlgeschlagenen Auftrag auf „wartet“ zurück; doppelte Uploads vervielfachen ihn nicht.

## Übernahme, Lebenszeichen und Abschluss

Die Auftragsübernahme erfolgt unter `BEGIN IMMEDIATE`. Solange ein gültiger laufender Import- oder Analyseauftrag existiert, übernimmt kein weiterer Worker ein anderes Buch in derselben Ablage. Importaufträge haben bei der Auswahl Vorrang; laufende Analysen werden dafür nicht unterbrochen. Jeder Versuch bekommt einen neuen zufälligen Claim-Token und 30 Sekunden Gültigkeit. Der Worker verlängert diese über einen eigenen Thread alle zwei Sekunden. Fortschrittsupdates werden begrenzt geschrieben, üblicherweise höchstens alle 500 ms oder beim Wechsel des Arbeitsschritts.

Verlängerungen und Ergebnis-Commits verlangen einen noch gültigen Token. Abgelaufene Tokens können nicht nachträglich erneuert werden. Ein ersetzter Worker kann daher weder den Status noch das Ergebnis seines Nachfolgers überschreiben. Verliert ein Worker seinen Anspruch, verwirft er sein vorläufiges Ergebnis.

Das eigentliche Einlesen benötigt keine offene SQLite-Schreibtransaktion. Der Abschluss prüft den Bearbeitungsanspruch erneut und schreibt Ergebnis und Auftragsstatus in einer Transaktion. Ein bereits vorhandener erfolgreicher Snapshot einschließlich späterer manueller Korrekturen hat Vorrang. Änderungen an Kapitelgrenzen aktualisieren den abschließenden Prüfstatus desselben Auftrags atomar.

Nach Ablauf einer Lease wird ein unterbrochener Auftrag erneut eingereiht. Nach drei solchen Versuchen wird er mit einem verständlichen Fehler angehalten. Erwartete Dateifehler und unerwartete Verarbeitungsfehler enden sofort als „fehlgeschlagen“; keine Endlosschleife. Ungeprüfte Exception-Texte und Tracebacks werden weder im Auftrag gespeichert noch in der Oberfläche ausgegeben.

## Checkpoints und Oberfläche

Der Import-Checkpoint ist der vollständige Extraktionssnapshot pro Buchversion. Unvollständige Extraktionen werden erneut berechnet. Fortschrittszähler sind keine Teil-Checkpoints. Seit Schritt 8 speichert die separate Analysequeue zusätzlich validierte Kapitelzusammenfassungen, Buchkontext und Zitatabschnitte; siehe [book-analysis.md](book-analysis.md).

`GET /books/local/<book-id>/status` liefert nur den öffentlichen Status: Zustand, Bezeichnung, Arbeitsschritt, Absatzfortschritt, Versuche, Kapitelzahl und eine bereinigte Fehlermeldung. Claim-Token, lokale Pfade und Buchtexte fehlen in dieser Antwort. Die Importseite fragt den Status alle zwei Sekunden ab und lädt nach Abschluss das Ergebnis. Aktive Tabellenzeilen der Buchübersicht aktualisieren sich alle drei Sekunden. Formulare mit manuellen Kapitelkorrekturen werden dadurch nicht automatisch neu geladen.

Die lokale Anwendung bleibt auf Loopback beschränkt; Schreibaktionen prüfen weiterhin die Herkunft. Für späteren Serverbetrieb braucht es gesonderten Zugangsschutz. Supabase-Schreibzugriffe sind noch nicht angebunden.

## Geprüfte Fehlerfälle

- Zwei gleichzeitige Übernahmeversuche und mehrere wartende Bücher.
- Abgelaufener Anspruch, veralteter Ergebnis-Commit und verspätetes Lebenszeichen.
- Drei unterbrochene Versuche, anschließender manueller Neustart.
- Übernahme alter Uploads und fertiger, manuell korrigierter Textstände.
- Bereinigte Fehlermeldung bei unerwarteter Exception.
- Lesbarer Status und bedienbare Buchübersicht während laufender Extraktion.
- Echter separater Windows-Prozess: abgebrochenen Auftrag übernehmen, Prozess erzwungen beenden, automatischer Neustart, neuen Upload verarbeiten und regulär ohne verbleibenden Worker herunterfahren.
