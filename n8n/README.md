# n8n-Workflows für Instagram-Carousels

Der Generator `tools/build_n8n.py` erzeugt zwei inaktive, credential-freie
Importdateien:

- `book-promotion-review.json`: Textfreigabe und Carousel-Freigabe über Telegram.
- `book-promotion-auto.json`: derselbe Render- und Publish-Ablauf ohne wartende Freigaben.

Der alte Einzelbild-Export `book-promotion.json` wurde entfernt. Der Generator
schreibt ausschließlich lokale Dateien und überträgt keinen Workflow an n8n.

## Ablauf

Beide Workflows reservieren einen v5-Entwurf mit ihrem festen
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

## Credentials nach dem Import

Alle IDs beginnen absichtlich mit `REPLACE_`. In n8n müssen folgende vorhandene
oder neue Credentials zugeordnet werden:

| Credential | Verwendung |
|---|---|
| Supabase API | Reservierung, Transitionen, Tabellenzugriffe sowie private Storage-Uploads, Downloads, Signed URLs und Deletes. Ausschließlich einen serverseitigen Secret-/Service-Key verwenden. |
| one.intelligence API | `one.intelligence Chat Model` für Caption und Bildprompt. |
| Cloudflare HTTP Header Auth | `Authorization: Bearer …` für Workers AI/FLUX. |
| Telegram API | Review-Trigger, Vorschauen, Freigaben und Statusmeldungen. |
| Instagram-Token | Im Node `Refresh token for insta` den Platzhalter `MIT_RICHTIGEM_KEY_ERSETZEN` durch den gültigen langlebigen Instagram-Token ersetzen. |

Der Git-Export enthält nur den gut sichtbaren Platzhalter. Der Refresh-Node läuft
sowohl bei einer neuen Veröffentlichung als auch bei der Wiederaufnahme eines
bereits laufenden Publish-Vorgangs. Alle Instagram-Nodes der Ausführung verwenden
anschließend ausschließlich den frisch zurückgegebenen Token. Das
Supabase-Credential muss auf dasselbe Projekt zeigen wie `Config.supabase_url`.
Die Workflows setzen voraus, dass Schema v5 und beide Storage-Buckets bereits
vorhanden sind.

## Sicherer erster Import

1. Gewünschte JSON-Datei in einen neuen n8n-Workflow importieren.
2. Alle Platzhalter-Credentials zuordnen und im Node `Refresh token for insta`
   `MIT_RICHTIGEM_KEY_ERSETZEN` durch den neuen Instagram-Token ersetzen.
3. Im Node `Config` Konto, Chat und API-Version prüfen.
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
