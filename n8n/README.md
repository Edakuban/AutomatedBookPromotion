# n8n-Workflows für Instagram-Carousels

Der Generator `tools/build_n8n.py` erzeugt zwei inaktive, credential-freie
Importdateien:

- `book-promotion-review.json`: Textfreigabe und Carousel-Freigabe über Telegram.
- `book-promotion-auto.json`: derselbe Render- und Publish-Ablauf ohne wartende Freigaben.
- `book-promotion-reel-prompt-helper.json`: authentifizierter On-Demand-Webhook
  für den bewährten Begleittext-/Bildprompt-Vertrag.
- `book-promotion-reel-publisher.json`: KI-freier Multi-Plattform-Workflow mit
  täglicher FIFO- und stündlicher Termin-Warteschlange.

Der alte Einzelbild-Export `book-promotion.json` wurde entfernt. Der Generator
schreibt ausschließlich lokale Dateien und überträgt keinen Workflow an n8n.

## Reel-Produktion und Publisher

Kapitel-Reels benötigen zusätzlich Schema v9; Gesamt-Teaservideos benötigen
v10 (`20261002080336_book_teaser_sources.sql`, nach v9). Beide Migrationen
sind nur lokal vorbereitet und werden nicht beim Einreihen automatisch ausgeführt.
Der aktualisierte Publisher bleibt mit v8-Zitat-Reels kompatibel. Für Gesamt-Teaser
prüft er eine separate Obergrenze von 300 MiB und 600 Sekunden; normale Reels
behalten 50 MiB und 60 Sekunden. Die lokale Queue prüft tatsächliche MP4-Dimensionen,
Dauer, Audio und Hash vor jedem Upload.

Gesamt-Teaser gehen aktuell an YouTube (16:9/9:16) oder Instagram (9:16).
Facebook/TikTok für Gesamt-Teaser sind noch nicht unterstützt. YouTube wird über
die Videos-API ohne hinzugefügtes `#Shorts` veröffentlicht; Hochformat bis 180 Sekunden
klassifiziert YouTube selbst als Short. Für ein eindeutig normales Video 16:9 wählen.
Den geänderten Publisher neu importieren und `publish_enabled=false` bis zum
freigegebenen Dry-Run beibehalten. Hier wurden keine Live-Veröffentlichungen gestartet.

Die kreative Reel-Produktion bleibt lokal. Das Webprojekt friert Zitat, Titel,
Beschreibung, Bild- und Videoprompt sowie das geprüfte MP4 in `reel_assets`
ein. Für jedes gewählte Ziel entsteht in `reel_publications` ein eigener
Snapshot mit Konto, Optionen und entweder täglichem FIFO-Modus oder festem
Zeitpunkt. Das MP4 liegt digestbasiert in Supabase Storage oder Cloudflare R2.

Der Prompt-Helper übernimmt dabei exakt den bisherigen sicheren Vertrag:
`POST /webhook/bookpromo-reel-prompts` erhält `{quote, book}` und gibt nach
Schema- und Längenprüfung `{addition, image_prompt, caption}` zurück. Der
Webhook muss in n8n einem eigenen Header-Auth-Credential mit langem Secret
zugeordnet werden. Zitat und Buchprofil gelten im Systemprompt ausdrücklich als
nicht vertrauenswürdige Daten; das unveränderte Zitat wird deterministisch in
die Caption eingebaut. Der Helper veröffentlicht nichts und schreibt nicht in
Supabase.

Der Publisher enthält keinerlei KI-Nodes. Um 20 Uhr Europe/Berlin verarbeitet
er pro Plattform den ältesten freien Daily-Eintrag; der zweite Trigger prüft
stündlich fällige Termine. Sein Ablauf ist:

1. Pro Instagram, Facebook, YouTube und TikTok atomar höchstens ein Ziel claimen.
2. Für Supabase eine Signed URL erzeugen. Bei R2 zuerst die gespeicherte
   Custom-Domain testen; fehlt sie oder schlägt sie fehl, SigV4-URL aus den
   n8n-Umgebungsvariablen erzeugen.
3. MP4-Größe, `ftyp`-Header und SHA-256 gegen das Manifest prüfen.
4. Den jeweiligen Plattformadapter mit ausschließlich dem gespeicherten Titel,
   der Beschreibung und dem Options-Snapshot ausführen. TikTok verwendet
   `FILE_UPLOAD`, sodass keine verifizierte Domain erforderlich ist.
5. Externe ID und optionalen Permalink revisionsgeschützt speichern.
6. Das MP4 erst löschen, wenn alle Ziele bestätigt `published` oder bewusst
   `cancelled` sind; anschließend Cleanup als `deleted` quittieren.

Ab der ersten Plattform-Schreiboperation führen Timeouts und unklare Antworten
zu `publish_uncertain`; es gibt keinen automatischen zweiten Publish-Versuch.
Auch bei einer unklaren DB-Antwort auf `published` wird das Storage-Objekt nicht
gelöscht. Ein fehlgeschlagener Storage-Delete lässt den Datensatz gefahrlos auf
`cleanup_pending` stehen. Ein noch aktiver `publishing`, `processing` oder
`publish_uncertain`-Datensatz blockiert den nächsten Claim derselben Plattform,
bis er geprüft wurde.

`publish_enabled:false` ist auch hier der Importstandard. Da der Claim erst
hinter diesem Gate liegt, verändert ein Dry Run die Warteschlange nicht.

Asset-/Zieltabellen und RPCs kommen mit Schema v8 aus den Migrationen
`20260927120000_reel_multiplatform_storage.sql` und
`20260927130000_reel_claim_all_accounts.sql`. Das konfigurierte Cloud-Projekt
ist bereits auf diesem Stand.

## Ablauf

Beide Workflows reservieren einen v6-Entwurf mit ihrem festen
`execution_mode`, erzeugen Caption und Grundmotiv und normalisieren das Motiv
ohne Verzerrung auf 1080 × 1350 Pixel. Danach entstehen:

1. Hero mit Titel-Overlay und Kapitelmarke.
2. Ein bis acht Zitat-Slides mit Titel-Overlay, halbtransparentem dunklem
   Labelblock und weißer Arial-Schrift, aber ohne Kapitelmarke.
3. Der bereits vollständig in Python gerenderte CTA-Slide.

Das Zitat wird mit `Intl.Segmenter('de')` zuerst an Satzgrenzen geteilt. Nur ein
für eine Slide zu langer Satz wird zunächst an Klauselzeichen und zuletzt an
Wortgrenzen aufgeteilt. Eine Integritätsprüfung verhindert Textverlust. Das
fertige Manifest muss 3 bis 10 positionssortierte JPEGs enthalten.

Alle Slides werden unter
`<post-id>/<revision>/<position>-<sha256>.jpg` in den privaten Supabase-Bucket
`book-promotion-media` geladen. Der dauerhafte CTA und das Titel-Overlay bleiben
getrennt im privaten Bucket `book-promotion-assets`.

Im Review-Workflow werden nach dem Upload kurzlebige Signed URLs erzeugt und
alle Slides mit Telegram `sendMediaGroup` als Album angezeigt. Eine separate
Nachricht enthält die Buttons für Freigabe, Bild-Retry, Text-und-Bild-Retry und
Verwerfen. Callback-Daten binden Post-ID, Revision und Action-Token kompakt an
genau diese Vorschau.

Nach Freigabe beziehungsweise automatischer DB-Freigabe läuft der gemeinsame
Instagram-Zweig:

1. `begin_publish` beansprucht den Entwurf.
2. Für jedes Medium entsteht unmittelbar vorher eine neue Signed URL.
3. Ein anonymer GET prüft JPEG, 1080 × 1350, Größenlimit und SHA-256.
4. Child-Container werden einzeln erstellt; jede ID wird sofort über
   `bookpromo_media_container` gespeichert und bis `FINISHED` geprüft.
5. Der Parent enthält die sortierten Child-IDs, ausschließlich dort die Caption
   und `is_ai_generated=true`; seine ID wird revisionsgeschützt gespeichert.
6. Der Parent wird bis `FINISHED` geprüft und genau einmal veröffentlicht.
7. Erst `published` speichert Instagram-Medien-ID und Permalink.
8. Danach werden temporäre Storage-Objekte gelöscht und über
   `bookpromo_media_cleanup` als `deleted` bestätigt.

Unklare Antworten nach Beginn der Instagram-Verarbeitung wechseln zu
`publish_uncertain`. Dabei werden sämtliche Dateien und Container-IDs bewahrt;
der Workflow veröffentlicht nicht automatisch erneut. Schlägt nur das Cleanup
fehl, bleibt der Post `published` und kann gefahrlos über `resume_post_id`
erneut bereinigt werden.

Im Auto-Workflow führen eindeutige Fehler des Bildmodells und Antworten ohne
Bild zu einem neuen Zitat. Der fehlgeschlagene Post bleibt mit `status=failed`,
dem unveränderten `quote_id` und der gekürzten Provider-Meldung in `error`
gespeichert. Dasselbe Zitat wird für diesen Modus am selben Tag nicht erneut
reserviert. `Config.max_image_quote_attempts` begrenzt die Kette einschließlich
des ersten Versuchs auf fünf. Netzwerk-Timeouts werden wegen unklarer externer
Kosten oder Seiteneffekte nicht automatisch wiederholt. Der Review-Workflow
wechselt nach einer bereits erteilten Textfreigabe ebenfalls nicht ungefragt zu
einem anderen Zitat.

## Credentials nach dem Import

Alle IDs beginnen absichtlich mit `REPLACE_`. In n8n müssen folgende vorhandene
oder neue Credentials zugeordnet werden:

| Credential | Verwendung |
|---|---|
| Supabase API | Reservierung, Transitionen, Tabellenzugriffe sowie private Storage-Uploads, Downloads, Signed URLs und Deletes. Ausschließlich einen serverseitigen Secret-/Service-Key verwenden. |
| one.intelligence API | `one.intelligence Chat Model` für Caption und Bildprompt. |
| Cloudflare HTTP Header Auth | `Authorization: Bearer …` für Workers AI/FLUX. |
| Telegram API | Review-Trigger, Vorschauen, Freigaben und Statusmeldungen. |
| Instagram Long-Lived Token | Im Reel-Publisher und den Carousel-Flows ausschließlich den Platzhalter `MIT_RICHTIGEM_KEY_ERSETZEN` im Node `Refresh token for insta` ersetzen. Die Content-Publishing-Nodes verwenden den frisch zurückgegebenen Token ohne eigenes Credential. |
| Facebook Header Auth | `Authorization: Bearer …` für Page Reels. |
| YouTube OAuth2 | Google OAuth2 mit `youtube.upload`-Scope. |
| TikTok Header Auth | `Authorization: Bearer …` mit `video.publish`-Scope. |
| Reel Prompt Header Auth | Eigenes langes Shared Secret für den On-Demand-Webhook; nicht mit dem Supabase-Key identisch. |

Der Git-Export enthält nur den gut sichtbaren Platzhalter. Der Refresh-Node läuft
sowohl bei einer neuen Veröffentlichung als auch bei der Wiederaufnahme eines
bereits laufenden Publish-Vorgangs. Alle Instagram-Nodes der Ausführung verwenden
anschließend ausschließlich den frisch zurückgegebenen Token. Das
Supabase-Credential muss auf dasselbe Projekt zeigen wie `Config.supabase_url`.
Die Workflows setzen voraus, dass Schema v6 und beide Storage-Buckets bereits
vorhanden sind.

Für temporäre R2-GET-URLs werden im Node `Config` die vier sichtbaren
Platzhalter `r2_endpoint`, `r2_access_key_id`, `r2_secret_access_key` und
`r2_signed_url_ttl_seconds` gesetzt. Damit funktioniert der Signer auch auf
n8n-Installationen, die `$env` in Code-Nodes blockieren. Die Werte erscheinen
dadurch allerdings im Workflow und in manuellen Ausführungsdaten; der Workflow
darf nur für vertrauenswürdige n8n-Benutzer sichtbar sein. Der Git-Export
enthält ausschließlich Platzhalter. GET und DELETE werden unmittelbar vor der
Anfrage mit diesen Werten signiert; ein zusätzliches AWS-Credential am
Delete-Node ist nicht erforderlich. Der Prompt-Helper benötigt ausschließlich
Reel Prompt Header Auth und das vorhandene one.intelligence-Credential.

## Sicherer erster Import

1. Gewünschte JSON-Datei in einen neuen n8n-Workflow importieren.
2. Alle Platzhalter-Credentials zuordnen; Plattformkonten kommen aus den
   eingefrorenen Supabase-Zielen, nicht aus dem Workflow.
3. `Config.supabase_url` prüfen und die vier R2-Platzhalter ausschließlich in
   der importierten n8n-Kopie ersetzen.
4. `publish_enabled:false` beibehalten.
5. Review-Workflow zunächst nur manuell testen; den Zeittrigger bei Bedarf
   deaktivieren. Telegram-Callbacks benötigen einen aktiven Workflow und einen
   erreichbaren Telegram-Webhook.
6. Kurzes Zitat (3 Slides), langes Zitat, Retry und Overflow über acht
   Zitat-Slides prüfen.
7. Erst nach einem erfolgreichen Review-Dry-Run `publish_enabled:true` mit einem
   kontrollierten professionellen Instagram-Testkonto verwenden.
8. Erst nach erfolgreichem Review-Livetest den Auto-Workflow aktivieren.

`Config.graph_version` ist im Export auf `v26.0` gesetzt. Vor dem Livetest die
aktuell für die eingerichtete Instagram-App unterstützte Version und die
benötigten Content-Publishing-Berechtigungen in Metas Dokumentation prüfen.

Der Review-Trigger sollte einen eigenen Telegram-Bot verwenden oder in eine
bereits vorhandene zentrale Callback-Routing-Lösung integriert werden: pro Bot
ist nur ein aktiver Telegram-Webhook möglich.

## Dry Run und Wiederaufnahme

`publish_enabled:false` stoppt nach vollständiger Generierung, Upload und
Freigabe vor `begin_publish`. Es wird nichts an Instagram übertragen.

Für einen sicheren manuellen Wiederanlauf `Config.resume_post_id` setzen und
`Manual test` starten. Bei `generating_text` oder `generating_image` ist
zusätzlich `resume_generation:true` nötig; vorher muss ausgeschlossen sein,
dass der alte KI-Aufruf noch läuft. Das kann erneut Modellkosten verursachen.

`publishing` setzt bei bereits gespeicherten Child- oder Parent-IDs fort, ohne
sie neu zu erzeugen. `published`, `discarded` und `failed` dürfen manuell nur
den idempotenten Cleanup erneut ausführen. `publish_uncertain` wird absichtlich
nicht automatisch fortgesetzt und muss zuerst extern abgeglichen werden.

Die Workflow-Einstellungen speichern weder erfolgreiche noch fehlgeschlagene
Ausführungsdaten. So landen kurzlebige Signed URLs nicht in der n8n-Historie.
Für Debugging nur vorübergehend und bewusst abweichende Einstellungen verwenden.

## Lokale Entwicklung

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\build_n8n.py
node tools\check_n8n.mjs
.\.venv\Scripts\python.exe -m pytest tests\test_n8n_workflow.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_reel_cloud.py -q
node tools\check_n8n_reels.mjs
```

Der Generator entfernt beim erfolgreichen Lauf den Legacy-Export und erzeugt
beide JSONs reproduzierbar neu. Die Checks kompilieren sämtliches Code-Node-
JavaScript und prüfen Graph, Credentials, Rendervertrag, Supabase-Lifecycle und
die Trennung von Instagram-Child und -Parent.

Relevante Primärdokumentation:

- [n8n Telegram: Send Media Group](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.telegram/message-operations/)
- [n8n HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)
- [Supabase: private Dateien und Signed URLs](https://supabase.com/docs/guides/storage/serving/downloads)
- [Supabase: Standard-Uploads](https://supabase.com/docs/guides/storage/uploads/standard-uploads)
- [Meta: Instagram Content Publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing)
