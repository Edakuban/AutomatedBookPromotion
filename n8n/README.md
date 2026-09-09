# Book Promotion für n8n 2.35.4

Importdatei: [book-promotion.json](book-promotion.json). Zielinstanz: `https://n8n.oi.destination.one/`. Der Export ist **inaktiv** und wurde wie gewünscht nicht auf dem Server importiert. Er enthält keine API-Schlüssel. Das Instagram-Konto und der Ziel-Chat stammen aus dem bereitgestellten Feiertagsflow. Der Nutzer hat den Zielchat als eigenen Privat-Chat bestätigt; die Freigabeperson `1182925320` wurde in Supabase eingetragen und die Promotion-Einstellung aktiviert.

Zusätzlich liegt lokal `outputs/n8n/book-promotion.private.json` mit dem Starttoken aus dem Feiertagsflow bereit. Diese Datei enthält einen Schlüssel und liegt im von Git ausgeschlossenen Ausgabeordner. Sie ist ansonsten identisch zur öffentlichen Vorlage. Der Generator erzeugt ausschließlich die Vorlage mit Platzhalter.

## Was bereits funktioniert und geprüft ist

- Das Supabase-Schema v4 ist im Projekt `AutomatedBookPromotion` angelegt. Titel-Overlays liegen bei Bedarf im privaten Bucket `book-promotion-assets`; `book_profile.overlay_path` enthält den Objektpfad. Beim Reservieren ergänzt Supabase die Kapitelposition. Der Workflow lädt das Overlay mit dem vorhandenen Supabase-Credential, setzt es auf das JPEG und zeichnet rechts unten `Kapitel X` auf einen dunklen Hintergrund.
- Buchstände lassen sich atomar aus dem Tool übertragen; die Originalzitate werden auch in SQL gegen die Kapiteltexte geprüft.
- Der Workflow reserviert ein Zitat, erstellt den Text, wartet auf Textfreigabe, erstellt das Bild, wartet auf Bildfreigabe und veröffentlicht erst danach.
- Der Tagesstart liegt bei 06:00 Uhr `Europe/Berlin`. Ein offener Entwurf oder ein bereits angelegter Tageslauf verhindert einen zweiten Tagesentwurf.
- Jede Entscheidung prüft Entwurf, Revision und erlaubten Telegram-Nutzer. Alte oder doppelte Antworten lösen keine zweite Aktion aus.
- Der JSON-Graph und das enthaltene JavaScript sind lokal geprüft; der Datenbankvertrag wurde in PostgreSQL getestet. Ein n8n-Live-Durchlauf mit deinen Credentials und ein Instagram-Testpost stehen noch aus.

## Einrichtung

1. Das lokale Tool neu starten, dein Buch analysieren und anschließend auf der Buchseite **Buchstand nach Supabase übertragen** wählen. Das aktuell vorhandene lokale Buch war bei der Einrichtung noch nicht analysiert. Die ursprüngliche DOCX bleibt lokal; übertragen werden Kapiteltexte, Zitate und Einstellungen.
2. In n8n einen neuen Workflow anlegen und **Import from File** mit `book-promotion.json` verwenden. Den bestehenden Feiertagsflow unverändert lassen.
3. Die unten genannten Credentials in den betroffenen Nodes auswählen. Alle `REPLACE_…`-Verweise sind absichtlich Platzhalter.
4. In Supabase die Promotion-Einstellung prüfen: Das Instagram-Konto `35706486682300403` ist inzwischen **aktiv** im Modus `random_book`, mit Ziel-Chat und Freigabeperson `1182925320`. Auch das Buch muss in seinen Bucheinstellungen vorgemerkt und dieser Stand übertragen sein. Der zuletzt lesend geprüfte Stand enthält das aktive Buch „Verzerrte Wahrheit“ und 229 grundsätzlich geeignete Zitate; offene Reservierungen können die konkrete Auswahl zusätzlich einschränken.
5. Im Node **Config** die Angaben prüfen. `publish_enabled: false` zunächst beibehalten. Ein erster Test kann Text und Bild freigeben, beendet sich aber vor Instagram mit einem Hinweis. Der Entwurf bleibt im Zustand `approved` erhalten.
6. Telegram-Callbacks benötigen einen erreichbaren Telegram-Trigger. Für einen vollständigen Test muss der Workflow veröffentlicht/aktiv sein. Für diesen ersten Test den Node **Daily 06 Berlin** deaktivieren, damit nur der manuelle Start und Telegram-Entscheidungen laufen. Nach erfolgreicher Abnahme den Tagesstart wieder einschalten.

### Credentials zuordnen

| Credential im Export | In n8n einrichten | Nodes |
|---|---|---|
| Supabase (`REPLACE_supabaseApi`) | Supabase-Credential: Host `https://aqfemzwrkzimzakqiwls.supabase.co`, Secret Key aus deiner `.env` | Native Lese-Nodes sowie HTTP-Nodes für Reservierung und Transitionen, überall dasselbe Credential |
| one.intelligence (`REPLACE_oneIntelligenceApi`) | Vorhandenes Credential des `one.intelligence Chat Model` aus dem Feiertagsflow auswählen; dort Domain und Open-WebUI-Key verwalten | `one.intelligence Chat Model` |
| OpenAI (`REPLACE_openAiApi`) | Vorhandenes OpenAI-Credential auswählen | `Generate image` |
| Cloudflare Workers AI (`REPLACE_httpHeaderAuth`) | Header-Auth-Credential mit Header `Authorization` und Wert `Bearer <WORKERS_AI_TOKEN>` | `Cloudflare FLUX image`, `Cloudflare caption` |
| Telegram (`REPLACE_telegramApi`) | Passendes Telegram-Bot-Credential auswählen | Telegram-Trigger sowie alle Telegram-Nodes |

Supabase, one.intelligence, OpenAI, Cloudflare und Telegram haben jeweils eigene Credentials. Die beiden Cloudflare-Nodes verwenden dasselbe Header-Auth-Credential. Das native Supabase-Credential setzt `apikey` und Bearer-Header automatisch; ein lesender Zugriff mit genau diesen Headern wurde am Zielprojekt erfolgreich geprüft. Instagram nutzt auf Nutzerwunsch einen direkt übergebenen Token.

### Vorhandene Nodes verwenden

**Text:** Aktiv ist `Text request → Generate caption`. `Generate caption` ist eine Basic LLM Chain mit deinem installierten `CUSTOM.lmChatOneIntelligence` und einem Structured Output Parser. Das Textmodell steht weiterhin in **Config → text_model**. `Cloudflare caption` liegt als nicht verbundene Alternative im Workflow; Account und Modell stehen in **Config → cloudflare_account_id/cloudflare_text_model**. Zum Wechsel die Verbindung von **Text request** zu **Generate caption** entfernen und stattdessen **Text request** mit **Cloudflare caption** verbinden. Niemals beide Wege gleichzeitig anschließen, da sonst derselbe Entwurf doppelt verarbeitet würde. **Validate caption** versteht sowohl das one.intelligence-Ergebnis als auch die Cloudflare-REST-Hülle. Es verlangt genau Begleittext und Bildprompt; danach werden das Originalzitat unverändert ergänzt und die Instagram-Textlänge geprüft. Gesprächsspeicher und Agent-Werkzeuge werden für diesen einzelnen Auftrag nicht benötigt. Der lokale Import/Analyse-Dienst verwendet weiterhin seine eigene Open-WebUI-Anbindung.

**Supabase:** `Read draft` und `Read resume draft` verwenden native Supabase-Nodes. Reservierung und Zustandswechsel bleiben HTTP-Aufrufe von Datenbankfunktionen: Der native Node unterstützt nur Tabellenoperationen, keine RPC-Aufrufe. Diese Funktionen sichern Reservierungen und Freigaben atomar gegen doppelte Ausführungen ab. Auch die HTTP-Nodes verwenden das native Supabase-Credential unter „Predefined Credential Type“. **Config → supabase_url** und Credential-Host müssen dasselbe Projekt bezeichnen.

**Bild:** Aktiv ist `Image request → Cloudflare FLUX image → Convert to File`. Der native OpenAI-Node **Generate image** bleibt als nicht verbundene Alternative im Workflow. Zum Wechsel die Verbindung von **Image request** zu **Cloudflare FLUX image** entfernen und stattdessen **Image request** mit **Generate image** verbinden; dessen Ausgang ist bereits mit **Convert to JPEG** verbunden. Niemals beide Generatoren gleichzeitig anschließen. Account und Modell für FLUX stehen in **Config → cloudflare_account_id/cloudflare_image_model**. Der von OpenAI oder FLUX gelieferte quadratische 1024×1024-Ausgang wird als JPEG normalisiert, mittig auf 816×1020 zugeschnitten und ohne Verzerrung auf das Instagram-Hochformat 1080×1350 (4:5) skaliert. Der Textprompt hält bildwichtige Motive deshalb im mittleren 80-Prozent-Bereich. Wenn `book_profile.overlay_path` gesetzt ist, lädt **Download title overlay** die private, ebenfalls 1080×1350 große PNG-Datei aus Supabase, **Composite title overlay** setzt sie bei Position 0/0 auf das Bild und anschließend zeichnen zwei Edit-Image-Nodes den Kapitel-Hintergrund und `Kapitel X` rechts unten. Ohne Overlay bleibt der Ablauf gleich und ergänzt nur die Kapitelmarke. Dieser Ablauf benötigt GraphicsMagick auf dem n8n-Server. Vor dem Upload prüft der Workflow JPEG-Kennung, exakte Abmessungen und die 8-MiB-Grenze; die Dateiübergabe unterstützt auch n8ns ausgelagerten Binärspeicher. Fehler in FLUX, der Base64-Dateikonvertierung oder der übrigen Bildkette führen zum gespeicherten Fehlerstatus und lösen keine automatische Bild-Neuerstellung aus.

Nach dem Wechsel vom quadratischen Overlay muss jedes Buch mit aktiviertem Titel-Overlay einmal erneut über **Buchstand nach Supabase übertragen** synchronisiert werden. Dadurch erhält Supabase die neue 1080×1350-Datei; ein altes 1024×1024-Overlay wird vom Workflow mit einer verständlichen Fehlermeldung abgewiesen.

### Instagram ohne Credential

Im Node **Refresh token for insta** unter Query Parameters den Wert von `access_token` setzen. In der öffentlichen Vorlage steht `REPLACE_INSTAGRAM_LONG_LIVED_TOKEN`; in der privaten Importdatei ist der vorhandene Starttoken bereits übernommen. Seine aktuelle Gültigkeit wurde nicht live geprüft.

Nach Bildfreigabe und nur bei `publish_enabled=true` wird wie im Feiertagsflow `GET https://graph.instagram.com/refresh_access_token` mit `grant_type=ig_refresh_token` aufgerufen. **Instagram access token** prüft die Antwort. Container-Erstellung und Veröffentlichung übergeben anschließend `access_token` als Formularfeld, die Statusabfrage als Query-Parameter. Alle drei lesen `$('Instagram access token').first().json.access_token`; ein Instagram-Credential wird nicht benötigt.

Erst nach erfolgreicher Token-Erneuerung wird der Entwurf als `publishing` beansprucht. Scheitert die Erneuerung, bleibt er `approved` und kann nach Behebung über `resume_post_id` fortgesetzt werden. Der erneuerte Token wird nicht in Buch- oder Postdaten gespeichert. Er steht in der aktuellen Ausführung zur Verfügung; der Starttoken im Refresh-Node wird nicht automatisch umgeschrieben. Beim Teilen eigener Workflow-Exporte auch direkt eingetragene Tokens und Ausführungsdaten entfernen.

Für Telegram möglichst einen eigenen Bot für diese Buch-Promotion verwenden. Ein Bot kann nur einen aktiven Telegram-Webhook besitzen; bei Wiederverwendung eines bereits über einen Telegram-Trigger betriebenen Bots müssen dessen Callbacks in einen gemeinsamen Workflow integriert werden. Der vorhandene Feiertagsflow benutzt Warteaktionen; seine Credential-Zuordnung wird nicht automatisch übernommen.

### Promotion-Modus und Freigabeperson

`bookpromo_reserve` mit `outcome: inactive` bedeutet: Für **Config → account_id** fehlt die Zeile in `promotion_settings` oder ihr Feld `active` ist noch `false`. Der Export zeigt dafür jetzt im Node **No quote context** einen konkreten Einrichtungshinweis. Vor Aktivierung auch `telegram_user_id` eintragen. `publish_enabled: false` in Config verhindert weiterhin Instagram-Posts, erlaubt aber Text-/Bildtests mit Freigaben.

In der Supabase-Tabelle `promotion_settings` die vorhandene Zeile bearbeiten:

- `telegram_user_id`: deine tatsächliche numerische Telegram-Benutzer-ID als Text. Der Chat wird zusätzlich geprüft. Die App nimmt nicht automatisch an, dass der Ziel-Chat dieselbe ID hat.
- `mode = random_book`: gleichverteilt ein geeignetes aktives Buch wählen, anschließend bevorzugt ein noch unbenutztes Zitat daraus.
- `mode = fixed_book`: zusätzlich `fixed_book_id` auf die ID des gewünschten übertragenen Buchs setzen.
- `active = true`: erlaubt die Anlage neuer Entwürfe. Der n8n-Zeitplan und `publish_enabled` sind davon getrennt.
- `reuse_after_days = null`: bereits veröffentlichte Zitate nicht wiederholen. Optional positive Sperrfrist in Tagen setzen.
- `discard_cooldown_days`: verworfene Zitate zeitweise ausschließen, Standard sieben Tage.
- `max_generations`: höchstens fünf zusätzliche Text-/Bild-Neuerstellungen pro Entwurf, gemeinsam gezählt.

## Freigaben und Fortsetzung

**Textphase:** Telegram zeigt den vollständigen späteren Posttext. „Text freigeben“ startet erst die Bildgenerierung. „Text neu“ behält das Originalzitat bei. „Verwerfen“ beendet den Entwurf.

**Bildphase:** Telegram zeigt das Bild und anschließend noch einmal den unveränderten freigegebenen Text. „Bild neu“ ändert nur das Bild. „Text + Bild neu“ führt zurück zur Textphase; das neue Bild entsteht wieder erst nach Textfreigabe. „Bild freigeben und posten“ erreicht die separate Veröffentlichungssperre.

Für den kontrollierten Live-Test `publish_enabled: true` setzen. Einen bereits freigegebenen Testentwurf kannst du über dessen `id` in **Config → resume_post_id** und **Manual test** fortsetzen. Danach `resume_post_id` wieder leeren. Diese Fortsetzung ist nur bei manueller Ausführung wirksam; der Tagesstart übernimmt sie nicht. Gespeicherte Text-/Bildvorschauen können so erneut zugestellt werden, zum Beispiel nach einem Telegram-Sendefehler.

### Offene Entwürfe weiterverwenden

Die manuelle Fortsetzung prüft `$execution.mode === 'test'`. n8n liefert in Ausdrücken `test` oder `production`; der Wert `manual` aus früheren Exporten war falsch und hat die Fortsetzung verhindert. Bei einem bereits eingerichteten Workflow in **Manual resume?** und **Resume preview** den Vergleich des Ausführungsmodus von `'manual'` auf `'test'` korrigieren. Credentials und sonstige Einstellungen bleiben dabei erhalten.

**Draft available?** leitet einen neuen oder bereits offenen Entwurf weiter. Ein Entwurf mit gespeicherter Text- oder Bildvorschau zeigt diese erneut zur Freigabe. Bei `generating_text` oder `generating_image` bleibt der Ablauf still stehen, damit keine möglicherweise schon laufende KI-Anfrage doppelt ausgelöst wird. Diese Fälle über n8n-Ausführungen abgleichen und nur bei sicher beendeter Anfrage mit `resume_post_id` und `resume_generation: true` manuell fortsetzen.

Schon ein einzelner Test von **Reserve quote** legt einen Entwurf an. Weitere Aufrufe liefern diesen als `existing` zurück. Der False-Zweig schützt vor doppelter Generierung und Veröffentlichung; **No quote context** zeigt bei manuellen Tests jetzt Entwurfs-ID, Zustand und den Hinweis zur Fortsetzung. Den Filter nicht pauschal auf `created` oder `existing` erweitern.

Für einen vorzeitig gestoppten Test:

1. In n8n **Executions** die vorige Ausführung prüfen und einen noch laufenden Versuch beenden. Bereits gestartete Anbieteranfragen können nach einem Abbruch weiterlaufen; insbesondere eine Bildgenerierung zunächst beim Anbieter beziehungsweise anhand der Ausführungsdaten prüfen.
2. Die vorhandene Entwurfs-ID in **Config → resume_post_id** setzen.
3. Nur bei `generating_text` oder `generating_image` zusätzlich **resume_generation: true** setzen. Das erlaubt ausdrücklich eine erneute Generierung des reservierten Entwurfs und kann erneut Modellkosten verursachen. Wenn der Test vor dem KI-Node gestoppt wurde, wird dessen erster Aufruf damit nachgeholt.
4. Über **Manual test** starten. `publish_enabled: false` erlaubt weiterhin einen Test ohne Instagram-Veröffentlichung. Anschließend `resume_post_id` leeren und `resume_generation: false` zurücksetzen.

`publishing`, `publish_uncertain` und bereits abgeschlossene Entwürfe bleiben von dieser Wiederaufnahme ausgeschlossen. Text- und Bildfreigaben werden weiterhin anhand des gespeicherten Zustands geprüft; eine wiederholte Bildgenerierung braucht bereits freigegebenen Text.

Der Entwurf bleibt ohne Telegram-Antwort offen und blockiert neue Tagesentwürfe. Es gibt keine automatische Freigabe nach Wartezeit. Bei vorübergehendem Datenbankfehler kann der ursprüngliche Button erneut verwendet werden; wenn die Entscheidung schon gespeichert wurde, wird sie als veraltet verworfen. Die Vorschau kann anschließend über die manuelle Fortsetzung wieder angezeigt werden.

Ein Generationfehler setzt den Entwurf auf `failed`; es gibt keine automatische kostenpflichtige Wiederholung. Ein abgebrochener n8n-Lauf während `generating_text`/`generating_image` verlangt einen manuellen Abgleich der Ausführung, bevor ein neuer Generierungsversuch gestartet wird. Die Wiederaufnahme solcher Generierungen erfordert ausdrücklich `resume_generation: true` und eine manuelle Ausführung. Der Fehlerstatus und gegebenenfalls gespeicherte Medien bleiben nachvollziehbar.

## Instagram und Medien

Der Export verwendet wie der vorhandene Flow `graph.instagram.com` mit dessen Version `v25.0`: Token erneuern, Container anlegen, Container-ID speichern, höchstens 20 Statusprüfungen mit je 15 Sekunden Abstand, erst bei `FINISHED` veröffentlichen. Token-Erneuerung und direkte Parameterübergabe sind aus dem Feiertagsflow übernommen. Ein bereits abgelaufener oder anderweitig ungültiger Starttoken muss durch einen gültigen ersetzt werden.

Generiert wird zunächst ein quadratisches Bild mit Cloudflare FLUX Schnell; der getrennte OpenAI-Fallback nutzt `gpt-image-2` mit mittlerer Qualität. Die nachfolgenden Nodes erzeugen daraus ein JPEG mit exakt 1080×1350 Pixeln und höchstens 8 MiB. Das Bild wird nach dem Muster des Referenzflows zu Uguu hochgeladen. **Dies ist die vorläufige Medienablage:** Eine dort abgelaufene Bild-URL kann vor einer späten Freigabe unbrauchbar werden. Vor unbeaufsichtigtem Dauerbetrieb eine dauerhaft geeignete Ablage festlegen oder deren tatsächliche Aufbewahrungsfrist gegen den Freigabeablauf prüfen. Es werden keine Originalmanuskripte zu Uguu übertragen.

Ein unklarer Fehler nach Beginn der Instagram-Verarbeitung setzt den Entwurf auf `publish_uncertain` und hält die Reservierung. Auch ein n8n-Abbruch im Zustand `publishing` blockiert neue Tagesentwürfe. In diesen Fällen anhand der gespeicherten Container-ID beziehungsweise Instagram-Medien extern abgleichen. **Nicht einfach den Publish-Node erneut ausführen.** Erst ein bestätigter Medien-Erfolg darf mit der Transition `published` gespeichert werden. Die automatische externe Wiederabgleich-Funktion ist noch nicht enthalten.

## Entwicklung und Quellen

`tools/build_n8n.py` erzeugt den Export reproduzierbar. Änderungen vorzugsweise dort vornehmen und neu erzeugen. Lokale Prüfungen:

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/build_n8n.py
node tools/check_n8n.mjs
node tools/check_integration.mjs .uv-cache/pglite-0.5.8/package/dist/index.js
.\.venv\Scripts\python.exe tools/check_chapter_label.py --font C:\Windows\Fonts\arial.ttf
```

Die Kapitelmarke nutzt **Edit Image v1** (n8n 2.35.4). Die Koordinaten sind
Textanfang und Grundlinie; `horizontalAlignment`/`verticalAlignment` aus neueren
Node-Versionen stehen dort nicht zur Verfügung. Die X-Position berücksichtigt
die Anzahl der Ziffern in Arial 34 px, die Grundlinie liegt bei Y=1255. Der letzte
Check benötigt ImageMagick und rendert die tatsächlichen Workflow-Parameter für
ein-, zwei- und dreistellige Kapitel sowie „Buchauszug“. Er prüft die sichtbaren
Pixelgrenzen und erzeugt `outputs/n8n/label-verification/preview.png`.
Beim Aktualisieren der Live-Node den vollständigen Parameterblock einschließlich
`operation: text`, Textausdruck, Font und Größe setzen; anschließend gespeicherte
und veröffentlichte Version prüfen. Ein unvollständiger Block kann auf die
Standardoperation `border` zurückfallen und trotz erfolgreichem Lauf keinen Text
zeichnen.

- [n8n: Telegram-Operationen](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.telegram/message-operations/)
- [n8n: HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)
- [n8n: Supabase-Node und Grenzen](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.supabase/)
- [n8n: Edit Image](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.editimage/)
- [OpenAI: Bildgenerierung](https://developers.openai.com/api/docs/guides/image-generation)
- [Supabase: API-Absicherung](https://supabase.com/docs/guides/api/securing-your-api)

Die konkreten Telegram-, HTTP-, Supabase-, LLM-Chain-, Parser-, OpenAI- und Edit-Image-Node-Felder wurden gegen den offiziellen n8n-Quellstand `n8n@2.35.4` geprüft. Typ und Parameter des benutzerdefinierten one.intelligence-Nodes stammen aus deinem Referenzexport; dessen Zusammenspiel mit der Chain muss noch auf der Zielinstanz getestet werden.
