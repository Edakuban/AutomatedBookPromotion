# Vorhandener Feiertagsflow als Vorlage

Stand: 08.09.2026. Grundlage ist der vom Nutzer bereitgestellte n8n-JSON-Export. Er wurde lokal gelesen, nicht importiert oder ausgeführt. Dieses Dokument hält seine Struktur und die übernommenen Anforderungen fest. Der daraus entwickelte Buch-Workflow liegt inzwischen als [importierbare JSON-Datei mit Anleitung](../n8n/README.md) vor. Der private Referenzexport wird nicht ins Projekt kopiert.

## Nutzervorgabe

**Erst Text prüfen lassen, dann Bild, dann Post.**

1. Täglich um 06:00 Uhr in `Europe/Berlin` Buch und Originalzitat reservieren.
2. Vollständigen Posttext mit dem unveränderten Zitat erstellen und in Telegram zeigen.
3. **Text freigeben** startet die Bildgenerierung. **Text neu** erstellt nur neuen Begleittext bei gleichem Zitat und verlangt wieder Textfreigabe. **Verwerfen** beendet den Entwurf.
4. Bild erstellen und in Telegram mit dem bereits freigegebenen Text zeigen.
5. **Bild freigeben und posten** veröffentlicht. **Bild neu** behält den freigegebenen Text und verlangt erneut Bildfreigabe. **Text neu / Beides neu** führt zurück zur Textphase; nach neuer Textfreigabe wird ein neues Bild erzeugt. **Verwerfen** beendet den Entwurf.
6. Erst nach bestätigter Veröffentlichung Zitatnutzung und Postzeitpunkt speichern.

## Wiederverwendbare Bausteine aus dem Export

| Vorhandener Baustein | Verwendung für Bücher |
|---|---|
| `Schedule Trigger` mit Stunde 6 und manueller Trigger | Täglicher Start sowie kontrollierte Testausführung; Zeitzone ausdrücklich setzen |
| `one.intelligence Chat Model` und Structured Output Parser | Als Modell und Parser an einer Basic LLM Chain für Begleittext/Bildprompt eingebunden |
| Telegram `sendAndWait`, `Approved?`, `Try again?` | Zwei getrennte Freigabestufen mit Wiederholungs-/Verwerfen-Zweigen |
| `Send a photo message` | Bildvorschau nach erfolgreicher Textfreigabe |
| `openai create image based on another` | Derselbe native OpenAI-Node mit Aktion Generate Image; anschließend JPEG-Konvertierung für Instagram |
| Upload + `Extract Uguu Public URL` | Muster zur Bereitstellung einer abrufbaren Medien-URL; endgültigen Medien-Speicher bei der Integration festlegen |
| `Create Instagram Media Container` | Container aus freigegebenem Text und Bild anlegen |
| `Wait for Instagram Processing` + Statusabfrage + `Is Container Ready?` | Verarbeitung abwarten und `FINISHED` prüfen |
| `Publish Instagram Media` + `All done!` | Bestätigt veröffentlichen und Erfolg melden |

Im Referenzflow erfolgt zuerst eine Feiertagsauswahl per Telegram-Freitext; nach der Bildgenerierung wird Bild/Text gemeinsam freigegeben. Für die Buchversion wird daraus ausdrücklich eine Prüfung des vollständigen Posttexts vor jeder ersten Bildgenerierung. Feiertagssuche und feste Buch-/Coverdaten werden durch Daten aus der Buchverwaltung ersetzt.

## Anforderungen an die Übernahme

- Freigaben an gespeicherte Entwurfs-ID, Phase und Vorschauversion binden. Eine Textfreigabe ist keine Veröffentlichungsfreigabe; alte Antworten dürfen spätere Fassungen nicht freigeben.
- Den freigegebenen Text persistent einfrieren. Eine Bildwiederholung übernimmt exakt diesen Text und dasselbe Originalzitat.
- Den Rücksprung „Try Again?“ auf die betroffene Generierungsphase begrenzen. Im Referenzexport führt er zum anfänglichen JavaScript-Knoten und damit erneut in die Feiertagssuche.
- Reservierung, Generierungsstände und IDs dauerhaft speichern; wiederaufgenommene Runs nicht allein über positionsbasierte Merge-Nodes oder Gesprächsspeicher zuordnen.
- Das Container-Polling besitzt im Export einen Rückweg für jeden Nicht-`FINISHED`-Status. Für die Buchversion maximale Wartezeit/Versuche und explizite Behandlung endgültiger Fehler vorsehen.
- Den Token-Refresh-Baustein erst nach Prüfung des verwendeten Instagram-Zugangs übernehmen. Credential-Verweise getrennt einrichten; keine privaten Werte aus dem Export in einen versionierten Workflow kopieren.
- Ablauf von Telegram-Wartezeiten ausdrücklich behandeln: ohne Antwort keine Freigabe und keine Veröffentlichung. Autorisierte Entscheider und veraltete Vorschauaktionen prüfen.
- Bei unklarem Veröffentlichungserfolg extern abgleichen; kein blindes erneutes Veröffentlichen.

Der Datenbankvertrag wurde mit Schema v2 auf getrennte Text- und Bildfreigaben erweitert und im Zielprojekt eingerichtet. Reservierung, Revisionen, Freigabeperson und Übergänge sind getestet. Der neue Workflow ist inaktiv exportiert; ein n8n-Live-Test steht noch aus. Token-Erneuerung und direkte Übergabe von `access_token` wurden inzwischen auf Nutzerwunsch übernommen. Der automatische externe Abgleich bei unklarem Publish-Erfolg bleibt offen; die Grenzen sind in der [n8n-Anleitung](../n8n/README.md) beschrieben.
