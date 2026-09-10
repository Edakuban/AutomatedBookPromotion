# Implementierungsplan: Instagram-Carousels

Stand: 10.09.2026

Implementierungsstand: Phase 1 (lokale Assets und CTA-Renderer) ist im Python-Projekt umgesetzt und getestet. Supabase-Sync, Schema und n8n bleiben bewusst für die folgenden Phasen offen.

## 1. Ziel und verbindliche Produktentscheidungen

Die Buchpromotion erzeugt und veröffentlicht ausschließlich Instagram-Carousels aus 3 bis 10 Bildern. Pro Post wird nur ein KI-Grundmotiv generiert. Alle weiteren Bilder werden deterministisch aus diesem Motiv beziehungsweise aus lokal gepflegten Buchassets abgeleitet. Das bisherige Einzelbildformat wird nicht weitergeführt.

Verbindlicher Aufbau eines Carousels:

1. **Hero-Slide:** Grundmotiv wie bisher, mit vorhandenem Buchtitel-Overlay und Kapitellabel.
2. **Zitat-Slides:** 1 bis maximal 8 Seiten mit demselben Grundmotiv, einem dunklen halbtransparenten Labelblock, weißem Zitattext und vorhandenem Buchtitel-Overlay. Diese Slides erhalten kein Kapitellabel.
3. **CTA-Slide:** In der Python-Anwendung vorgerendertes Bild mit einem aus dem Frontcover erzeugten 2.5D-Buch, einem frei gepflegten CTA-Text, dem vorhandenen Buchtitel-Overlay und dem hochgeladenen Logo rechts unten.

Damit gilt: `1 Hero + 1–8 Zitat-Slides + 1 CTA = 3–10 Carousel-Elemente`.

Weitere Festlegungen:

- Ausgabeformat aller Slides: exakt 1080 × 1350 Pixel (4:5), JPEG, sRGB, maximal 8 MiB.
- Das Originalzitat bleibt unverändert. Das Aufteilen und Umbrechen erfolgt deterministisch und nicht durch ein Sprachmodell.
- Die bestehende Caption bleibt zunächst unverändert: Originalzitat, KI-Begleittext, Titel/Autor und Zieladresse.
- Ein Klick auf „Bild neu“ erzeugt ein neues Grundmotiv und daraus das komplette Carousel neu. Caption und Zitat bleiben dabei unverändert.
- Ein Klick auf „Text + Bild neu“ erzeugt zuerst eine neue Caption beziehungsweise einen neuen Bildprompt und anschließend ein komplett neues Carousel.
- Es entstehen zwei getrennte n8n-Hauptworkflows: ein Review-Workflow mit Telegram-Freigaben und ein Auto-Workflow ohne Wartezustände.
- Beide n8n-Exporte werden aus gemeinsamen Builder-Funktionen erzeugt, damit Rendering, Validierung und Instagram-Publishing nicht doppelt gepflegt werden.
- Alle postbezogenen Carousel-JPEGs werden vor Freigabe in einem privaten Supabase-Storage-Bucket zwischengespeichert. Der bisherige externe anonyme Bildhost entfällt.
- Telegram und Meta erhalten ausschließlich kurzlebige Signed URLs. Gespeichert wird nur der private Objektpfad, niemals die Signed URL selbst.
- Postbezogene Bilder werden erst nach einem bestätigten Instagram-Publish und dem erfolgreichen Speichern des `published`-Zustands gelöscht. Bei `publish_uncertain` bleiben sie für den Abgleich erhalten.
- Zunächst bleibt höchstens ein nicht abgeschlossener Entwurf je Instagram-Konto erlaubt. Ein offener Review-Entwurf blockiert deshalb den Auto-Workflow für dasselbe Konto.
- Rückwärtskompatibilität mit dem bisherigen Einzelbild-Workflow ist nicht erforderlich. Vor der Schemaumstellung werden ausschließlich die wenigen vorhandenen Test-Posts und deren Veröffentlichungszustände gezielt entfernt; Bücher, Kapitel, Zitate und Buchprofile bleiben erhalten.

## 2. Technische Randbedingungen der Instagram API

Für jedes Carousel-Bild muss zuerst ein eigener Mediencontainer erstellt werden:

```text
POST /{IG_ACCOUNT_ID}/media
image_url=<PUBLIC_HTTPS_URL>
is_carousel_item=true
alt_text=<OPTIONALER_ALTERNATIVTEXT>
```

Danach wird ein übergeordneter Carousel-Container erstellt:

```text
POST /{IG_ACCOUNT_ID}/media
media_type=CAROUSEL
children=<CHILD_ID_1>,<CHILD_ID_2>,...
caption=<CAPTION>
is_ai_generated=true
```

Der Parent-Container wird bis `FINISHED` geprüft und anschließend veröffentlicht:

```text
GET /{CAROUSEL_CONTAINER_ID}?fields=status_code
POST /{IG_ACCOUNT_ID}/media_publish
creation_id=<CAROUSEL_CONTAINER_ID>
```

Zu beachten:

- Die Reihenfolge der Child-IDs ist die spätere Slide-Reihenfolge.
- `caption` wird nur am Parent-Container gesetzt.
- `is_ai_generated=true` wird nur am Parent-Container gesetzt. Bei Child-Containern darf dieser Parameter nicht gesendet werden.
- Die Bild-URLs müssen während der Container-Erstellung ohne Authentifizierung von Meta abrufbar sein.
- Container verfallen nach 24 Stunden.
- Vor Umsetzung die im Workflow konfigurierte Graph-Version von `v25.0` auf die dann aktuelle unterstützte Version prüfen; am 09.09.2026 dokumentiert Meta `v26.0`.
- Referenzen: [Meta Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing) und [IG User Media](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media).

## 3. Zielarchitektur und Datenfluss

```text
Python-Buchprojekt
  ├─ Frontcover hochladen
  ├─ Logo hochladen
  ├─ CTA-Text pflegen
  ├─ 2.5D-Cover und CTA-Slide rendern
  └─ Buchprofil + Titel-Overlay + CTA-Slide nach Supabase übertragen

Supabase
  ├─ unveränderlicher Buchprofil-Snapshot pro Post
  ├─ Post-/Freigabezustand
  ├─ geordnete post_media-Zeilen für alle Slides
  ├─ private, temporäre Carousel-JPEGs pro Post und Revision
  └─ Parent- und Child-Container-IDs

n8n Review oder Auto
  ├─ Zitat reservieren
  ├─ Caption und Bildprompt erzeugen
  ├─ ein Grundmotiv erzeugen und auf 1080 × 1350 normalisieren
  ├─ Hero- und Zitat-Slides rendern
  ├─ CTA-Slide laden
  ├─ alle Slides privat in Supabase Storage hochladen
  ├─ optional Telegram-Freigaben durchführen
  └─ Signed URLs → Child-Container → Parent-Container → Publish → Cleanup
```

## 4. Python: lokale Daten, Uploads und Buchverwaltung

### 4.1 Datenmodell erweitern

`BookDetails` in `src/bookpromo/management.py` erhält mindestens:

- `publication_mode: Literal["review", "auto"] = "review"`.
- `carousel_end_text: str` mit einem serverseitigen Längenlimit, zunächst maximal 500 Zeichen.

Binärdateien werden nicht direkt in `details_json` gespeichert. Stattdessen wird eine lokale Tabelle ergänzt:

```text
local_book_assets
  book_id
  kind                 -- cover_front | logo
  revision
  original_filename
  media_type
  sha256
  width
  height
  relative_path
  updated_at
  PRIMARY KEY (book_id, kind)
```

Aufgaben:

- [x] Tabelle ausschließlich bei einer Schreibaktion anlegen; lesende Seitenaufrufe verändern die Datenbank nicht.
- [x] Asset-Revisionen in den vorhandenen Konfliktschutz der Bucheinstellungen einbeziehen.
- [x] Ablage unter einem privaten Unterverzeichnis von `APP_DATA_DIR`, beispielsweise `data/book-assets/<book_id>/`.
- [x] Dateinamen serverseitig erzeugen und nie aus dem hochgeladenen Namen als Pfad übernehmen.
- [x] Alte Assets erst ersetzen, nachdem die neue Datei vollständig validiert und atomar gespeichert wurde.
- [x] Beim Löschen/Ersetzen keine Datei außerhalb des aufgelösten Asset-Verzeichnisses akzeptieren.

### 4.2 Uploadvalidierung

Für das Frontcover:

- [x] PNG, JPEG und WebP als Eingabe erlauben.
- [x] Datei tatsächlich mit Pillow dekodieren; Dateiendung und Browser-MIME-Type allein reichen nicht.
- [x] Maximale Dateigröße und maximale Pixelzahl definieren, um Dekompressionsbomben zu verhindern.
- [x] EXIF-Ausrichtung anwenden.
- [x] Alpha-Kanal erhalten, sofern vorhanden.
- [x] Eine Mindestauflösung festlegen, empfohlen mindestens 800 Pixel Breite.

Für das Logo:

- [x] PNG und WebP unterstützen; transparentes PNG in der Oberfläche empfehlen.
- [x] JPEG optional akzeptieren, aber auf den fehlenden transparenten Hintergrund hinweisen.
- [x] Dieselben Größen-, Pixel- und Dekodierungsprüfungen wie beim Cover verwenden.

### 4.3 Oberfläche

`src/bookpromo/templates/book_settings.html` wird erweitert um:

- [x] Checkbox „Instagram-Carousel verwenden“.
- [x] Auswahl „Freigabemodus: Telegram-Prüfung / automatisch veröffentlichen“.
- [x] Uploadfeld „Frontcover für 3D-Darstellung“.
- [x] Uploadfeld „Logo“.
- [x] Textarea „Text auf der Schlussseite“.
- [x] Anzeige des jeweils gespeicherten Dateinamens und der Bildabmessungen.
- [x] Vorschau des erzeugten CTA-Slides.
- [x] Verständliche Hinweise bei fehlenden Assets.

Formular-/Routing-Entscheidung:

- Die Textfelder bleiben im bestehenden revisionsgeschützten Einstellungsformular.
- Cover und Logo erhalten vorzugsweise eigene revisionsgeschützte Upload-Endpunkte. Dadurch ersetzt eine fehlerhafte Bilddatei nicht gleichzeitig gültige Texteinstellungen und umgekehrt.
- Die CTA-Vorschau erhält eine eigene Route und wird aus dem gespeicherten Stand gerendert.

### 4.4 Aktivierungsregeln

`promotion_enabled=true` darf nur gespeichert beziehungsweise nach Supabase übertragen werden, wenn:

- [x] ein gültiges Frontcover vorhanden ist,
- [x] ein gültiges Logo vorhanden ist,
- [x] `carousel_end_text` nicht leer ist,
- [x] das Buchtitel-Overlay erfolgreich gerendert werden kann,
- [x] der CTA-Slide erfolgreich als 1080 × 1350 JPEG erzeugt werden kann.

Bei unvollständiger Konfiguration bleibt die Promotion für dieses Buch deaktiviert. Es gibt keinen Rückfall auf einen Einzelbild-Post.

## 5. Python: 2.5D-Cover und CTA-Slide rendern

### 5.1 Neues Rendering-Modul

Die Zuständigkeiten sind auf zwei eindeutig benannte Module verteilt:

- `src/bookpromo/book_assets.py` kapselt Validierung, Revisionen und lokale Ablage von Frontcover und Logo.
- `src/bookpromo/carousel_end_slide.py` kapselt ausschließlich 2.5D-Cover, CTA-Schlussseite, Ausgabevalidierung und Render-Cache.

Das Rendering verwendet Pillow, das bereits Projektabhängigkeit ist. Hero- und Zitat-Slides sowie das vollständige Carousel entstehen weiterhin erst in n8n.

Öffentliche Kernfunktionen:

```text
render_cover_mockup(front_cover) -> RGBA image
render_carousel_end_slide(details, front_cover, logo, title_overlay) -> JPEG bytes
validate_carousel_end_slide(bytes) -> metadata
```

### 5.2 2.5D-Cover

- [x] Frontcover auf ein perspektivisches Viereck transformieren.
- [x] Einen künstlichen Buchrücken mit einer Breite von zunächst 8–12 % der Frontcoverbreite ergänzen.
- [x] Rückenfarbe aus dem linken Rand beziehungsweise einer robust bestimmten Coverfarbe ableiten.
- [x] Rücken gegenüber dem Frontcover abdunkeln und leicht mit einem Verlauf versehen.
- [x] Obere/untere Papierkante dezent ergänzen.
- [x] Weichen Schlagschatten mit `GaussianBlur` rendern.
- [x] Ergebnis auf transparentem RGBA-Hintergrund zurückgeben.
- [x] Keine Rückenbeschriftung erfinden; mit nur einem Frontcover ist es bewusst ein optischer 2.5D-Mockup und kein vollständiges Buchmodell.

### 5.3 CTA-Layout

Erste feste Layoutvorgabe:

- Canvas: 1080 × 1350.
- Titel-Overlay: unverändert bei 0/0 zusammensetzen.
- CTA-Text: links, unterhalb des reservierten Titelbereichs.
- Textdarstellung: ohne Kasten frei auf dem dunklen Hintergrund, mit einer schmalen vertikalen Akzentlinie in der Titelfarbe.
- 2.5D-Cover: rechts beziehungsweise rechtsmittig.
- Logo: rechts unten mit festem Sicherheitsabstand.
- Hintergrund: zunächst eine im Code definierte neutrale/markengerechte Fläche; spätere Konfigurierbarkeit ist nicht Teil des ersten Schritts.
- CTA-Text fest in Arial setzen, automatisch umbrechen und die Schriftgröße innerhalb definierter Grenzen reduzieren; überlange Einzelwörter mit sichtbarem `-` trennen und unterhalb der Mindestgröße mit einem verständlichen Validierungsfehler abbrechen.
- Alle Elemente innerhalb einer Safe Area von mindestens 64 Pixeln halten.

### 5.4 Determinismus und Cache

- [x] Digest über Cover, Logo, CTA-Text, Titel-Overlay und Renderer-Version bilden.
- [x] Fertige CTA-Slides unter einem digestbasierten Dateinamen speichern.
- [x] Bei unverändertem Digest den vorhandenen Render wiederverwenden.
- [x] Renderer-Version im Digest berücksichtigen, damit Layoutänderungen neue Dateien erzeugen.
- [x] Ausgabe explizit nach sRGB konvertieren und ohne problematische Metadaten als JPEG speichern.

## 6. Python → Supabase: Asset-Sync

Der bestehende explizite Buch-Sync bleibt die einzige Aktion, die lokale Assets nach Supabase überträgt.

Aufgaben:

- [ ] Vor dem Sync Titel-Overlay und CTA-Slide erneut aus dem aktuellen, revisionsgeschützten Stand rendern.
- [ ] CTA-Slide in den privaten Bucket `book-promotion-assets` hochladen.
- [ ] Digestbasierte Objektpfade verwenden, beispielsweise `<book_id>/carousel/<sha256>.jpg`.
- [ ] `book.profile` beziehungsweise der Sync-Payload erhält:
  - `overlay_path`
  - `publication_mode`
  - `carousel_end_text`
  - `carousel_end_slide_path`
- [ ] Cover und Logo bleiben lokal; nur das fertig gerenderte CTA-Bild wird übertragen.
- [ ] Der in `posts.book_profile` gespeicherte Snapshot muss diese Felder einfrieren, damit ein offener Entwurf durch spätere Buchänderungen nicht mutiert.
- [ ] Größen-, MIME- und Pfadprüfungen im Repository analog zum vorhandenen Overlay-Upload ergänzen.
- [ ] Schema-Version erhöhen und Fehlermeldung für einen veralteten Supabase-Stand ergänzen.

## 7. Supabase: Schema und Zustandsautomat

### 7.1 Bereinigende Migration

Eine neue Migration darf den noch nicht produktiv verwendeten Einzelbildvertrag bewusst ersetzen. Vor den strukturellen Änderungen werden ausschließlich vorhandene Zeilen aus `posts` und davon abhängige Test-Veröffentlichungsdaten entfernt. Buch-, Kapitel-, Zitat- und Profileinträge werden nicht gelöscht.

Die Migration führt mindestens ein:

```text
posts.execution_mode       -- review | auto

post_media
  id
  post_id                   -- FK posts(id)
  position                  -- 0 bis 9
  kind                      -- hero | quote | cta
  text_fragment             -- nur für quote, optional
  storage_path              -- privater, revisionsgebundener Objektpfad
  sha256
  signed_url_expires_at     -- optionales Auditfeld; URL selbst nicht speichern
  instagram_container_id
  status                    -- generated | uploaded | container_ready | cleanup_pending | deleted | failed
  error
  created_at
  updated_at
```

Constraints und Indizes:

- [ ] `unique(post_id, position)`.
- [ ] `unique(instagram_container_id)` für nichtleere Container-IDs.
- [ ] Position zwischen 0 und 9.
- [ ] Erlaubte `kind`- und `status`-Werte per Check-Constraint.
- [ ] Genau ein Hero bei Position 0 und genau ein CTA am Ende durch RPC-Validierung sicherstellen.
- [ ] Insgesamt 3 bis 10 Medien je vollständigem Manifest verlangen.
- [ ] RLS, Grants und Trigger entsprechend den bestehenden Anwendungstabellen konfigurieren.

- [ ] Vor der Bereinigung die Anzahl und IDs der zu löschenden Test-Posts protokollieren und gegen den erwarteten kleinen Testbestand prüfen.
- [ ] Ausschließlich `posts` und direkt davon abhängige Veröffentlichungsdaten löschen; keine Buch- oder Zitatdaten bereinigen.
- [ ] Die nicht mehr benötigte Spalte `posts.image_path` entfernen. Medien-URLs gehören ausschließlich in `post_media`.
- [ ] Nicht mehr benötigte Einzelbild-Constraints, Transitionzweige und Indizes entfernen statt sie als Kompatibilitätsschicht fortzuführen.
- [ ] `posts.instagram_container_id` speichert die Parent-Carousel-ID.
- [ ] Einen privaten, ausschließlich serverseitig beschreibbaren Bucket `book-promotion-media` für temporäre Postmedien anlegen.
- [ ] Objektpfade an Post-ID, Postrevision, Position und Digest binden, beispielsweise `<post_id>/<revision>/<position>-<sha256>.jpg`.
- [ ] Das dauerhafte `book-promotion-assets` mit Titel-Overlay und CTA-Quelle strikt vom temporären Postmedien-Bucket trennen.

### 7.2 Reservierung

`bookpromo_reserve` wird um den gewünschten Ausführungsmodus erweitert:

```text
bookpromo_reserve(p_account, p_day, p_execution_mode)
```

- [ ] Nur aktive Bücher mit passendem `publication_mode` auswählen.
- [ ] `execution_mode` beim Post speichern.
- [ ] Weiterhin höchstens einen offenen Post pro Konto zulassen.
- [ ] Einen bereits offenen Post nur an den Workflow mit demselben `execution_mode` zurückgeben.
- [ ] Bei einem offenen Post des anderen Modus einen eindeutigen Ausgang wie `blocked_by_other_mode` liefern.

### 7.3 Transitionen

Der Zustandsautomat wird modeabhängig erweitert:

- `text_ready`:
  - Review: `generating_text → awaiting_text_approval`.
  - Auto: Caption validieren, automatische Freigabe protokollieren und direkt zu `generating_image` wechseln.
- `media_ready`:
  - Manifest atomar validieren und `post_media` ersetzen.
  - Review: `generating_image → awaiting_image_approval`.
  - Auto: automatische Freigabe protokollieren und direkt zu `approved` wechseln.
- `retry_text` und `retry_image`:
  - vorhandene `post_media`-Zeilen und noch nicht veröffentlichte Containerinformationen kontrolliert verwerfen beziehungsweise als ersetzt markieren,
  - ersetzte, noch nicht an Instagram übergebene Storage-Objekte kontrolliert löschen,
  - Revision erhöhen,
  - alte Callback-Tokens ungültig machen.
- `carousel_container_ready`:
  - Parent-Container-ID nur einmal setzen.
- `published`:
  - Instagram-Medien-ID und Zeitpunkt speichern.
- `publish_uncertain`:
  - Parent- und Child-IDs erhalten, damit ein externer Abgleich möglich bleibt.

Automatische Freigaben werden eindeutig protokolliert, beispielsweise mit `approved_by = 'system:auto'`; sie dürfen niemals wie Telegram-Nutzerfreigaben aussehen.

### 7.4 Atomare Medien-RPCs

- [ ] Manifest und Child-Container nicht über ungeschützte Einzelupdates schreiben.
- [ ] RPC zum Speichern eines vollständigen Medienmanifests mit Post-ID, Revision und Action-Token einführen oder in `bookpromo_transition` integrieren.
- [ ] RPC zum einmaligen Speichern einer Child-Container-ID pro Position ergänzen.
- [ ] Wiederholte identische Aufrufe idempotent beantworten.
- [ ] Eine abweichende zweite Container-ID für dieselbe Position als Konflikt behandeln.
- [ ] RPC für revisionsgeschütztes Markieren von `cleanup_pending` und bestätigtem `deleted` ergänzen; die eigentliche Storage-Löschung erfolgt über die serverseitigen n8n-Credentials.

## 8. n8n: gemeinsame Builder-Bausteine

`tools/build_n8n.py` wird so umgebaut, dass gemeinsame Stufen nur einmal als Python-Funktionen definiert werden:

- [ ] `add_reservation_stage(mode)`
- [ ] `add_caption_stage(mode)`
- [ ] `add_carousel_render_stage()`
- [ ] `add_media_upload_stage()`
- [ ] `add_instagram_carousel_stage()`
- [ ] `add_failure_and_uncertain_routes()`
- [ ] `add_review_approval_stages()` nur für den Review-Workflow

Ausgaben:

- `n8n/book-promotion-review.json`
- `n8n/book-promotion-auto.json`

Der bisherige `n8n/book-promotion.json` wird durch die beiden neuen Exporte ersetzt und nach erfolgreicher Generierung der neuen Dateien entfernt. Es wird kein ausführbarer Legacy-Einzelbildworkflow mitgeführt.

Beide neuen Exporte bleiben standardmäßig inaktiv und enthalten keine Zugangsdaten.

## 9. n8n: Grundmotiv und 4:5-Normalisierung

Der eingecheckte Workflow fordert derzeit noch ein 1024 × 1024-Bild an, während das Python-Overlay bereits 1080 × 1350 misst. Vor der Carousel-Verarbeitung wird deshalb eine eindeutige Normalisierung eingebaut.

- [ ] Wenn das Bildmodell ein geeignetes Hochformat unterstützt, dieses anfordern.
- [ ] Generiertes Bild anschließend deterministisch auf 1080 × 1350 bringen: zentraler beziehungsweise bewusst gewählter Ausschnitt, danach Resize.
- [ ] Keine Verzerrung durch unabhängiges Skalieren von Breite und Höhe.
- [ ] Ergebnis nach JPEG mit sRGB und definierter Qualität konvertieren.
- [ ] Abmessungen, JPEG-Kennung und 8-MiB-Limit prüfen.
- [ ] Das normalisierte Grundmotiv als einzige Quelle für Hero- und Zitat-Slides verwenden.

## 10. n8n: Zitat in Slides aufteilen

### 10.1 Segmentierung

Ein Code-Node `Split quote into slides` erhält das unveränderte `quote_text`.

Algorithmus:

1. Absatzgrenzen erhalten.
2. Sätze mit `Intl.Segmenter('de', { granularity: 'sentence' })` segmentieren.
3. Sätze greedily zu einer Slide hinzufügen, solange das definierte Zeilenbudget nicht überschritten wird.
4. Einen zu langen einzelnen Satz zunächst an Semikolon, Doppelpunkt, Gedankenstrich oder Komma in sinnvolle Klauseln teilen.
5. Reicht das nicht, ausschließlich an Wortgrenzen teilen.
6. Niemals Zeichen aus dem Originaltext entfernen, ersetzen oder neu formulieren.
7. Höchstens acht Text-Slides erzeugen; bei Überschreitung den Entwurf mit einer eindeutigen Ursache stoppen.

### 10.2 Zeilenbudget

Der installierte Edit-Image-Node bricht Text anhand einer maximalen Zeichenzahl pro Zeile um. Der Code-Node bildet exakt denselben Umbruch vorab nach und speichert pro Slide:

```text
slide_text
slide_number
slide_count
wrapped_line_count
panel_x
panel_y
panel_width
panel_height
text_x
text_y
```

Erste Defaultwerte:

- maximale Zeilenlänge: anhand eines visuellen Tests mit der gewählten Schrift festlegen, Startwert 34–40 Zeichen,
- maximal 8 Zeilen pro Slide,
- Schriftgröße 50–56 px,
- Innenabstand 56–64 px,
- Blockfarbe `#111111DD`,
- weiße Schrift,
- abgerundete Ecken,
- optionale Seitenangabe außerhalb des Zitattexts.

### 10.3 Integritätsprüfung

- [ ] Alle `slide_text`-Werte in Reihenfolge wieder zusammensetzen und gegen das Originalzitat prüfen; nur zulässige, dokumentierte Zwischenraum-Normalisierung ignorieren.
- [ ] Leere Slides verhindern.
- [ ] Slideanzahl 1 bis 8 erzwingen.
- [ ] Panel muss vollständig innerhalb der Safe Area liegen.
- [ ] Sonderzeichen, deutsche Anführungszeichen, Gedankenstriche, Zeilenumbrüche und Emojis testen.

## 11. n8n: Slides rendern

### 11.1 Hero-Slide

- [ ] Normalisiertes Grundmotiv verwenden.
- [ ] Bestehendes transparentes Buchtitel-Overlay zusammensetzen.
- [ ] Bestehenden Kapitelblock und `Kapitel X` beziehungsweise `Buchauszug` ergänzen.
- [ ] Ergebnis als `position=0`, `kind=hero` kennzeichnen.

### 11.2 Zitat-Slides

Für jedes Item des Split-Nodes:

- [ ] Grundmotiv-Binary wiederverwenden.
- [ ] Dynamisch berechneten halbtransparenten Labelblock zeichnen.
- [ ] Zitatabschnitt in Weiß setzen.
- [ ] Optional kleine Seitenangabe ergänzen, jedoch getrennt vom Originaltext.
- [ ] Titel-Overlay zusammensetzen.
- [ ] Kein Kapitellabel zeichnen.
- [ ] Fortlaufende Position ab 1 setzen und `kind=quote` speichern.

### 11.3 CTA-Slide

- [ ] `carousel_end_slide_path` validieren.
- [ ] Fertiges privates JPEG mit dem Supabase-Credential laden.
- [ ] Abmessungen, JPEG-Kennung und Dateigröße prüfen.
- [ ] Als letzte Position und `kind=cta` markieren.
- [ ] Nicht nochmals Buchtitel oder Logo in n8n auftragen; der Python-Render ist bereits vollständig.

### 11.4 Manifest zusammenführen

- [ ] Hero, Zitat-Slides und CTA in stabiler Positionsreihenfolge zusammenführen.
- [ ] Genau 3 bis 10 Elemente verlangen.
- [ ] Eindeutige Dateinamen wie `<post_id>-00-hero.jpg`, `<post_id>-01-quote.jpg`, ... erzeugen.
- [ ] Für jedes Element einen passenden Alternativtext erzeugen, ohne Spoilerkontext oder interne Buchprofile zu veröffentlichen.
- [ ] Finales Manifest vor Upload und erneut vor Instagram-Erstellung validieren.

## 12. Temporäre Medienbereitstellung über Supabase Storage

Supabase Storage ersetzt den bisherigen externen anonymen Upload vollständig. Der Bucket `book-promotion-media` bleibt privat. Meta kann keine Supabase-Authorization-Header mitsenden und lädt `image_url` selbst vom angegebenen Server; deshalb erzeugt n8n erst unmittelbar vor dem jeweiligen externen Abruf eine zeitlich begrenzte Signed URL.

Verbindlicher Ablauf:

- [ ] Nach dem Rendern alle 3–10 finalen JPEGs in den privaten Bucket hochladen und jeden `storage_path` samt SHA-256 revisionsgeschützt in `post_media` speichern.
- [ ] Auch den CTA-Slide als postbezogene Kopie hochladen. Das dauerhafte CTA-Asset des Buchs wird niemals durch den Post-Cleanup gelöscht.
- [ ] Im Review-Flow erst nach erfolgreichem Upload das Telegram-Album senden und anschließend auf Freigabe warten.
- [ ] Für Telegram bei Bedarf eigene kurzlebige Signed URLs erzeugen; abgelaufene URLs jederzeit aus `storage_path` neu erzeugen.
- [ ] Nach der Freigabe für Meta neue Signed URLs erzeugen. Eine Gültigkeit von zunächst 60 Minuten ist konfigurierbar und muss den gesamten Child-Upload samt Polling abdecken.
- [ ] Signed URLs vor Übergabe an Meta mit einem normalen GET ohne zusätzliche Header prüfen; JPEG-Kennung, MIME-Type, Content-Length, SHA-256 und 1080 × 1350 erneut validieren.
- [ ] Signed URLs weder in `post_media` noch in Logs, Fehlermeldungen oder versionierten n8n-Exporten speichern. Persistiert werden nur private Objektpfade und optional der Ablaufzeitpunkt.
- [ ] Der bisherige Drittanbieter-Upload wird aus beiden neuen Workflows entfernt und nicht als Produktions-Fallback mitgeführt.

Bereinigung:

- [ ] Erst `media_publish` erfolgreich abschließen und eine gültige Instagram-Medien-ID erhalten.
- [ ] Danach den Post revisionsgeschützt als `published` speichern.
- [ ] Erst nach erfolgreicher Datenbanktransition alle postbezogenen Objekte aus `book-promotion-media` löschen und die Medienzeilen als `deleted` markieren.
- [ ] Schlägt die Löschung fehl, bleibt der Post `published`; die Medien wechseln auf `cleanup_pending` und werden idempotent erneut bereinigt.
- [ ] Bei Timeout oder unklarer Antwort von `media_publish` `publish_uncertain` speichern und keine Bilder löschen.
- [ ] Verworfene Entwürfe und eindeutig vor Instagram gescheiterte Revisionen ebenfalls bereinigen; sobald Container erstellt wurden, bis zur eindeutigen Klärung oder einem definierten Ablaufzeitraum aufbewahren.

Die Upload-, Signed-URL- und Cleanup-Implementierung wird im Builder gekapselt, damit Review- und Auto-Workflow denselben sicheren Lebenszyklus verwenden.

## 13. n8n Review-Workflow

Zieldatei: `n8n/book-promotion-review.json`.

Ablauf:

1. Manueller Trigger und optional eigener Zeitplan.
2. `bookpromo_reserve(..., 'review')`.
3. Caption und Bildprompt erzeugen und validieren.
4. Caption über Telegram freigeben lassen.
5. Grundmotiv und gesamtes Carousel erzeugen.
6. Alle Slides in Supabase Storage hochladen und Medienmanifest speichern.
7. Slides aus Supabase Storage in Telegram als Album anzeigen.
8. Danach eine separate Nachricht mit Inline-Buttons senden.
9. Bei Freigabe Child-Container, Parent-Container und Veröffentlichung ausführen.

Telegram-Details:

- [ ] Für die Vorschau `sendMediaGroup` mit allen 3–10 Bildern verwenden.
- [ ] Da ein Medienalbum keine geeignete gemeinsame Inline-Tastatur trägt, danach eine eigene Freigabenachricht senden.
- [ ] Buttons weiterhin an Post-ID, Revision, Vorschauversion und Action-Token binden.
- [ ] „Bild neu“, „Text + Bild neu“ und „Verwerfen“ auf das komplette Carousel anwenden.
- [ ] Alte Album- oder Callback-Antworten dürfen eine neuere Revision nicht freigeben.

Abnahme:

- [ ] Vor Textfreigabe wird kein Bildmodell aufgerufen.
- [ ] Vor Carousel-Freigabe wird nichts an Instagram gesendet.
- [ ] Alle Slides sind vor Freigabe in korrekter Reihenfolge sichtbar.
- [ ] Ein Bild-Retry verändert keine bereits freigegebene Caption.

## 14. n8n Auto-Workflow

Zieldatei: `n8n/book-promotion-auto.json`.

Ablauf:

1. Täglicher Trigger und manueller Testtrigger.
2. `bookpromo_reserve(..., 'auto')`.
3. Caption und Bildprompt erzeugen und streng validieren.
4. Modeabhängige automatische Textfreigabe in Supabase protokollieren.
5. Grundmotiv und gesamtes Carousel erzeugen.
6. Alle Medien und das Manifest validieren.
7. Alle Slides in Supabase Storage hochladen.
8. Modeabhängige automatische Medienfreigabe protokollieren.
9. Child-Container und Parent-Container erstellen.
10. Parent-Status prüfen, veröffentlichen, `published` speichern und temporäre Slides bereinigen.
11. Erfolg oder Fehler an Telegram melden; keine wartende Freigabenachricht erzeugen.

Sicherheitsregeln:

- [ ] Auto-Flow verarbeitet nur vollständig konfigurierte Bücher mit `promotion_enabled=true` und `publication_mode='auto'`.
- [ ] Fehlendes Cover, Logo, CTA-Bild oder Titel-Overlay verhindert die Veröffentlichung.
- [ ] Jede technische Bild- oder Manifestabweichung verhindert die Veröffentlichung.
- [ ] Kein automatischer erneuter Bildmodellaufruf nach einem Netzwerk-Timeout, wenn unklar ist, ob Kosten oder externe Seiteneffekte bereits entstanden sind.
- [ ] Bei unklarem Instagram-Publish `publish_uncertain` setzen und nicht erneut veröffentlichen.
- [ ] `publish_enabled=false` bleibt als Dry-Run-Gate erhalten.
- [ ] Der Auto-Workflow wird erst nach einem erfolgreichen Review-Livetest aktiviert.

## 15. Instagram-Carousel-Publishing in n8n

### 15.1 Child-Container

- [ ] Manifest positionssortiert laden.
- [ ] Für jedes Medium `POST /media` mit `image_url`, `is_carousel_item=true`, optional `alt_text` und Token senden.
- [ ] `image_url` unmittelbar davor als neue Signed URL aus dem privaten `storage_path` erzeugen.
- [ ] Keine Caption und kein `is_ai_generated` an Child-Container senden.
- [ ] Jede Child-ID unmittelbar revisionsgeschützt in `post_media` speichern.
- [ ] Einen bereits gespeicherten Child-Container bei einer Wiederaufnahme nicht erneut erzeugen.
- [ ] Bei Teilfehler Parent-Container noch nicht erstellen.
- [ ] Jeden Child-Container bis `FINISHED` prüfen; erst danach den Parent erstellen.

### 15.2 Parent-Container

- [ ] Erst nach vollständigen Child-IDs einen Parent mit `media_type=CAROUSEL` erstellen.
- [ ] `children` exakt in Positionsreihenfolge übergeben.
- [ ] Caption ausschließlich am Parent setzen.
- [ ] `is_ai_generated=true` ausschließlich am Parent setzen.
- [ ] Parent-ID als `posts.instagram_container_id` speichern.
- [ ] Bereits gespeicherten Parent bei einer Wiederaufnahme wiederverwenden.

### 15.3 Status und Publish

- [ ] Parent-Container bis `FINISHED` prüfen.
- [ ] Offizielle Polling-Empfehlung bei Implementierung erneut prüfen; aktuell empfiehlt Meta einmal pro Minute für maximal fünf Minuten.
- [ ] `ERROR` und `EXPIRED` explizit behandeln.
- [ ] Bei `FINISHED` genau einmal `media_publish` aufrufen.
- [ ] Bei Timeout oder unklarer Antwort nach Beginn der externen Verarbeitung `publish_uncertain` setzen.
- [ ] Instagram-Medien-ID speichern und, wenn verfügbar, Permalink nachladen.
- [ ] Carousel zählt nach bestätigtem Publish als eine Zitatnutzung.

### 15.4 Post-Publish-Cleanup

- [ ] Eine gültige Antwort von `media_publish` allein löst noch keine Löschung aus: zuerst Instagram-Medien-ID und Zustand `published` atomar speichern.
- [ ] Danach alle `storage_path`-Objekte dieser Postrevision idempotent löschen.
- [ ] Erfolgreich entfernte Medien als `deleted`, fehlgeschlagene Löschungen als `cleanup_pending` markieren.
- [ ] `publish_uncertain`, unbekannte Containerzustände und Datenbanktimeouts nach `media_publish` bewahren sämtliche Objekte für den externen Abgleich.
- [ ] Ein wiederaufgenommener Cleanup darf niemals `media_publish` erneut aufrufen.

## 16. Dokumentation und Konfiguration

- [ ] `README.md` um Carousel-Funktion und beide Betriebsmodi ergänzen.
- [ ] `docs/book-management.md` um Cover-/Logo-Uploads, CTA-Vorschau und Aktivierungsregeln ergänzen.
- [ ] `docs/supabase-integration.md` um neue Profilfelder, `post_media` und Storage ergänzen.
- [ ] `n8n/README.md` in Review- und Auto-Einrichtung gliedern.
- [ ] Medienhost, URL-Gültigkeit, Bereinigung und Dry-Run erklären.
- [ ] Graph-Version, erforderliche Instagram-Berechtigungen und professionelles Zielkonto dokumentieren.
- [ ] Keine Tokens, privaten URLs, Upload-Inhalte oder Credential-IDs in versionierte Exporte übernehmen.

## 17. Automatisierte Tests

### 17.1 Python

- [x] Speichern und erneutes Laden von `publication_mode` und CTA-Text.
- [x] Revisionskonflikte bei gleichzeitigen Text- und Assetänderungen.
- [x] Gültige PNG-/JPEG-/WebP-Uploads.
- [x] Ablehnung falscher Dateisignaturen, beschädigter Bilder, zu großer Dateien und zu vieler Pixel.
- [x] Sichere Pfade und atomarer Assettausch.
- [x] Deterministischer Digest und Cachetreffer.
- [x] CTA-Ausgabe exakt 1080 × 1350, JPEG, sRGB und unter 8 MiB.
- [x] Rendering mit Hoch-, Quer- und ungewöhnlich schmalen Frontcovern.
- [x] Transparente und nicht transparente Logos.
- [x] Sehr kurzer, mehrzeiliger und zu langer CTA-Text.
- [ ] Sync überträgt nur fertiges CTA-Bild und Profilpfade, nicht Cover-/Logo-Rohdateien.

### 17.2 Supabase/SQL

- [ ] Migration auf leerem Schema und auf dem aktuellen Schema mit dem erwarteten kleinen Testbestand.
- [ ] Die Migration entfernt ausschließlich Test-Posts und bewahrt Bücher, Kapitel, Zitate und Profile.
- [ ] `post_media`-Constraints und RLS/Grants.
- [ ] Review- und Auto-Transitionen.
- [ ] Automatische Freigaben sind eindeutig als solche gespeichert.
- [ ] Falscher Modus kann keinen vorhandenen Entwurf übernehmen.
- [ ] Idempotentes erneutes Speichern derselben Child-ID.
- [ ] Konflikt bei abweichender zweiter Child-ID.
- [ ] Manifest mit 2 oder 11 Elementen wird abgelehnt.
- [ ] Retry entfernt beziehungsweise entwertet veraltete Medienzustände.
- [ ] `publish_uncertain` bewahrt alle IDs.
- [ ] `publish_uncertain` bewahrt außerdem sämtliche privaten Storage-Objekte.
- [ ] `published` wird vor dem Storage-Cleanup gespeichert; ein Cleanup-Fehler ändert den Publish-Erfolg nicht.
- [ ] Wiederholter Cleanup ist idempotent und löscht niemals das dauerhafte Buchasset.

### 17.3 n8n-Strukturchecks

- [ ] Beide JSON-Exporte werden reproduzierbar aus dem Builder erzeugt.
- [ ] Keine Secrets oder privaten Credentials im Export.
- [ ] Review enthält beide Telegram-Freigabestufen.
- [ ] Auto enthält keine wartenden Telegram-Nodes.
- [ ] Beide verwenden identische Carousel-Render- und Instagram-Bausteine.
- [ ] Nur Parent-Container enthält Caption und `is_ai_generated`.
- [ ] Kein Drittanbieter-Upload ist mehr enthalten; Upload, Signed URL und Delete verwenden ausschließlich Supabase Storage.
- [ ] Signed URLs werden nicht im Workflow-Export oder in persistierten Medienzeilen abgelegt.
- [ ] Storage-Cleanup ist ausschließlich hinter einer bestätigten `published`-Transition erreichbar.
- [ ] Alle Child-Container enthalten `is_carousel_item=true`.
- [ ] Maximal zehn Manifestelemente gelangen zum Instagram-Zweig.
- [ ] Dry-Run-Gate blockiert `media_publish` in beiden Flows.

## 18. Manueller Testplan

In dieser Reihenfolge testen:

1. **Python ohne Supabase:** Cover und Logo hochladen, CTA-Vorschau prüfen, Anwendung neu starten und Persistenz kontrollieren.
2. **Python-Sync:** Titel-Overlay und CTA-Slide übertragen, Objektpfade und Profil-Snapshot lesen.
3. **Review-Dry-Run mit kurzem Zitat:** genau drei Slides erwarten.
4. **Review-Dry-Run mit langem Zitat:** mehrere Satz-Slides und korrekte Reihenfolge erwarten.
5. **Grenztest:** genau zehn Slides erzeugen.
6. **Overflow:** Zitat mit mehr als acht benötigten Text-Slides muss ohne Veröffentlichung verständlich fehlschlagen.
7. **Review-Retries:** Bild neu sowie Text + Bild neu prüfen; alte Telegram-Buttons testen.
8. **Instagram-Livetest Review:** privates/testbares professionelles Konto, Parent/Children und finalen Permalink prüfen.
9. **Unterbrechungstest:** Workflow nach einigen Child-Containern stoppen und sicher wiederaufnehmen.
10. **Publish-Timeout:** `publish_uncertain` und fehlenden automatischen Zweitpost verifizieren.
11. **Auto-Dry-Run:** keine Freigabenachrichten, aber vollständige Generierung und Validierung.
12. **Auto-Livetest:** zunächst ein einzelnes ausdrücklich dafür konfiguriertes Buch.

Bei jedem visuellen Test prüfen:

- Titel-Overlay nicht abgeschnitten.
- Kapitellabel ausschließlich auf dem Hero.
- Zitatblöcke gut lesbar und vollständig innerhalb der Safe Area.
- Keine Satzhälften auf unterschiedlichen Slides, sofern das Zeilenlimit dies zulässt.
- CTA-Text, Cover und Logo kollidieren nicht.
- Alle Slides haben identische Abmessungen und werden von Instagram nicht unerwartet beschnitten.

## 19. Implementierungsreihenfolge

Die folgende Reihenfolge minimiert blockierende Zwischenstände:

### Phase 1: Lokale Assets und CTA-Renderer

- [x] Lokales Assetmodell und sichere Uploads.
- [x] Neue Buchfelder und UI.
- [x] 2.5D-Cover-Renderer.
- [x] CTA-Slide und Vorschau.
- [x] Python-Tests für Phase 1.

**Fertig, wenn:** Ein Buch lokal vollständig für ein Carousel konfiguriert werden kann und ein reproduzierbares 1080 × 1350-CTA-JPEG entsteht.

### Phase 2: Sync und Supabase-Vertrag

- [ ] Asset-Sync und Profilfelder.
- [ ] Supabase-Migration mit `execution_mode` und `post_media`.
- [ ] Modeabhängige Reservierung und Transitionen.
- [ ] SQL- und Integrationstests.

**Fertig, wenn:** Review- und Auto-Entwürfe mit einem unveränderlichen Carousel-Profil-Snapshot reserviert und atomar fortgeschrieben werden können.

### Phase 3: Gemeinsame n8n-Carousel-Erzeugung

- [ ] 4:5-Normalisierung.
- [ ] Satzsegmentierung und Slidebudget.
- [ ] Hero-, Zitat- und CTA-Zweige.
- [ ] Manifest und Medienhosting.
- [ ] Privater Supabase-Upload, Signed-URL-Erzeugung und URL-Validierung.
- [ ] Strukturchecks.

**Fertig, wenn:** Ein manueller Dry-Run 3 bis 10 validierte, öffentlich erreichbare Slides in korrekter Reihenfolge erzeugt.

### Phase 4: Review-Workflow

- [ ] Telegram-Textfreigabe anbinden.
- [ ] Carousel als Telegram-Album senden.
- [ ] Medienfreigabe und Retries anbinden.
- [ ] Instagram-Child-/Parent-Erstellung.
- [ ] Post-Publish-Cleanup mit `cleanup_pending`-Wiederaufnahme.
- [ ] Review-Livetest.

**Fertig, wenn:** Das vollständige Carousel nach zwei gültigen Freigaben genau einmal auf Instagram erscheint und die Nutzung gespeichert wird.

### Phase 5: Auto-Workflow

- [ ] Auto-Orchestrierung aus denselben Builder-Bausteinen erzeugen.
- [ ] Automatische Freigaben und strenge Gates.
- [ ] Telegram-Erfolgs-/Fehlermeldung.
- [ ] Dry-Run und kontrollierter Livetest.

**Fertig, wenn:** Ein dafür konfiguriertes Buch ohne Benutzereingriff veröffentlicht wird und jeder Fehler vor beziehungsweise nach einem externen Seiteneffekt korrekt unterscheidbar bleibt.

### Phase 6: Härtung und Aktivierung

- [ ] Unterbrechungs- und Wiederaufnahmetests.
- [ ] Medienbereinigung.
- [ ] Tests für abgelaufene Signed URLs, `publish_uncertain` und fehlgeschlagenen Cleanup.
- [ ] Dokumentation abschließen.
- [ ] Auto-Workflow erst danach aktivieren.

## 20. Gesamtabnahme

Das Feature gilt als abgeschlossen, wenn:

- [ ] Cover, Logo und CTA-Text revisionssicher im Buchprojekt gepflegt werden können.
- [ ] Python aus einem Frontcover einen optisch überzeugenden 2.5D-Mockup und vollständigen CTA-Slide erzeugt.
- [ ] Jedes Carousel genau 3 bis 10 JPEGs im Format 1080 × 1350 enthält.
- [ ] Das Zitat unverändert und vollständig über maximal acht gut lesbare Labelblöcke verteilt wird.
- [ ] Nur der Hero ein Kapitellabel besitzt.
- [ ] Review- und Auto-Workflow aus gemeinsamem Builder-Code erzeugt werden.
- [ ] Telegram-Freigaben revisions- und nutzergebunden bleiben.
- [ ] Der Auto-Workflow nur ausdrücklich freigegebene Bücher verarbeitet.
- [ ] Instagram-Child- und Parent-Container idempotent und nachvollziehbar gespeichert werden.
- [ ] Unklare Publish-Ergebnisse niemals zu einem blinden Zweitpost führen.
- [ ] Es existiert kein ausführbarer Einzelbild-Publishingpfad mehr; unvollständig konfigurierte Bücher können nicht für Promotion aktiviert werden.
