# Implementierungsplan: vorbereitete Zitat- und Kapitelbilder für Carousels

Stand: 02.10.2026. Dieser Plan beschreibt eine neue, noch nicht umgesetzte
Erweiterung. Es wurden durch die Planung weder eine Supabase-Migration angewendet
noch n8n-Workflows importiert oder Veröffentlichungen ausgelöst.

## 1. Ziel

Ein lokal bewusst ausgewähltes Zitat- oder Kapitelbild kann ausdrücklich als
Grundbild für ein Carousel bereitgestellt werden. Der tägliche n8n-Workflow
verwendet die am engsten passende vorbereitete Quelle, ohne erneut ein
Bildmodell aufzurufen.

Wenn weder für das reservierte Zitat noch für sein Kapitel ein vorbereitetes
Bild verknüpft ist, bleibt die bisherige Live-Generierung vollständig erhalten.
Ein vorhandener, aber nicht abrufbarer oder ungültiger Verweis darf dagegen
niemals still durch die nächste Prioritätsstufe oder ein neues KI-Bild ersetzt
werden. Der Post wird gestoppt und eine konkrete Telegram-Meldung gesendet.

## 2. Verbindliche Produktentscheidungen

- Supabase bildet keinen Katalog aller lokalen Bildkandidaten ab.
- Pro Zitat und pro Kapitel wird jeweils höchstens ein ausdrücklich gewähltes
  Carousel-Grundbild remote gespeichert und verknüpft.
- Szenenbild, charakteroptimiertes Bild und eigener Upload bleiben als Kandidaten
  lokal. Nur die in der Oberfläche aktiv für das Carousel bestätigte Variante
  wird übertragen.
- Für Reels bleibt der bestehende Vertrag unverändert: Remote gespeichert und in
  die Veröffentlichungsqueue eingereiht wird ausschließlich das finale MP4.
  Reel-Szenenbilder oder Vorschaubilder werden nicht zusätzlich synchronisiert.
- Persistiert wird die Objektidentität aus Anbieter, Bucket, Objektpfad und
  SHA-256. Eine zeitlich begrenzte Signed URL wird niemals in der Datenbank
  gespeichert.
- Eine dauerhafte `public_url` darf nur gespeichert werden, wenn sie tatsächlich
  von einer konfigurierten R2-Custom-Domain stammt. Ohne Custom Domain bleibt sie
  `NULL`.
- Fehlt die Verknüpfung vollständig, erzeugt n8n wie bisher live ein Bild.
- Die feste Auswahlreihenfolge lautet: Zitatbild, sonst Kapitelbild, sonst
  Live-Generierung.
- Existiert eine Verknüpfung, ist sie verbindlich. Download-, Format-, Größen-,
  Dimensions- oder Hashfehler stoppen den Lauf und lösen eine Telegram-Meldung
  aus. Es gibt in diesem Fehlerpfad weder einen stillen FLUX-Aufruf noch einen
  automatischen Wechsel zu einem anderen Zitat.

## 3. Abgrenzung

Nicht Teil dieser Erweiterung sind:

- eine feste redaktionelle Reihenfolge von Zitaten,
- die Vorplanung eines konkreten Veröffentlichungsdatums,
- das Hochladen aller vorhandenen Bildvarianten,
- eine Galerie in Supabase,
- Änderungen am Inhalt oder an der Auswahl fertiger Reel-Videos,
- ein eigenes statisches Bild auf Buch-Teaser-Ebene,
- eine öffentliche Freigabe privater Supabase-Buckets,
- das Speichern oder Wiederverwenden abgelaufener Signed URLs.

Eine spätere redaktionelle Quote/Post-Queue kann auf dem hier beschriebenen
Medienvertrag aufbauen, ist aber ein separates Feature.

## 4. Soll-Ablauf

```text
Lokale Zitat-Reel- oder Kapitelbild-Werkstatt
  -> vorhandene Bildvariante beziehungsweise Kapitelbild auswählen
  -> exakten 4:5-Carousel-Ausschnitt prüfen
  -> "Für Carousel bereitstellen"
  -> genau dieses normalisierte JPEG hochladen und mit dem Zitat verknüpfen

n8n reserviert das Tageszitat
  -> Zitatbild vorhanden? dieses auswählen
  -> sonst Kapitelbild vorhanden? dieses auswählen
  -> ausgewählte Bildverknüpfung in den Post-Snapshot kopieren
  -> Caption und Fallback-Bildprompt erzeugen
  -> Snapshot enthält vorbereitetes Bild?
       ja   -> dasselbe Objekt frisch abrufen und streng prüfen
       nein -> bisherige FLUX-Generierung ausführen
  -> gemeinsamer Hero-/Zitat-/CTA-Renderpfad
  -> Telegram-Freigabe oder Auto-Publish wie bisher
```

Der Bildprompt wird weiterhin erzeugt und gespeichert. Er dient beim Fehlen eines
vorbereiteten Bildes der bisherigen Live-Generierung und bleibt für Diagnose und
manuelle Wiederholung verfügbar. Das Vorhandensein eines vorbereiteten Bildes
überspringt lediglich den externen Bildmodellaufruf.

## 5. Lokales Bild und Vorschau

### 5.1 Quelle

Ausgangspunkt ist entweder das aktuell ausgewählte Bild eines Zitat-Reel-Drafts
oder das ausdrücklich ausgewählte Bild des Kapitel-Teaser-Drafts:

- `selected_image_path` ist vorhanden,
- `selected_image_sha256` ist gültig und stimmt mit der Datei überein,
- `image_stale = false`,
- die Datei liegt innerhalb des erwarteten Draft-Verzeichnisses,
- Zitat beziehungsweise Kapitel gehören zum aktuellen übertragenen Buchstand,
- ein Zitatbild gehört zu einem nutzbaren Zitat.

Andere Kandidaten werden nicht automatisch übertragen. Ein Kapitelbild wird
nicht automatisch auf alle zugehörigen Zitate kopiert; es bleibt eine einzelne,
gemeinsam nutzbare Kapitelquelle.

### 5.2 Deterministische Normalisierung

Vor dem Upload erzeugt Python ein eigenes, reproduzierbares Carousel-Grundbild:

- EXIF-Orientierung anwenden,
- nach sRGB konvertieren,
- ohne Verzerrung auf das Seitenverhältnis 4:5 zuschneiden,
- auf exakt 1080 x 1350 Pixel skalieren,
- als JPEG mit definierter Qualität ausgeben,
- JPEG-Kennung, Dimensionen, Dateigröße und SHA-256 erneut prüfen.

Die Oberfläche zeigt genau dieses normalisierte Bild als Vorschau. Erst eine
ausdrückliche Aktion stellt es remote bereit. Damit sieht der Benutzer den
tatsächlichen Bildausschnitt, der später unter Titel-Overlay und Zitatblöcken
verwendet wird.

Für die erste Version genügt ein dokumentierter mittiger `cover`-Zuschnitt.
Eine verschiebbare Fokusposition wäre eine spätere UI-Erweiterung und darf den
ersten Vertrag nicht blockieren.

## 6. Speichervertrag

### 6.1 Unterstützte Anbieter

Der vorhandene Veröffentlichungs-Speicher wird wiederverwendet:

- `supabase`: privater Bucket `book-promotion-assets`,
- `cloudflare_r2`: konfigurierter privater R2-Bucket.

Der Objektpfad ist inhaltsadressiert, beispielsweise:

```text
carousel-sources/quotes/<book-id>/<quote-id>/<sha256>.jpg
carousel-sources/chapters/<book-id>/<chapter-id>/<sha256>.jpg
```

Ein erneuter Upload identischer Bytes bleibt dadurch idempotent.

### 6.2 URLs

In der Datenbank werden dauerhaft gespeichert:

- `storage_provider`,
- `storage_bucket`,
- `storage_path`,
- optionale stabile `public_url`,
- `media_sha256`,
- `size_bytes`, `width`, `height` und `mime_type`.

Nicht gespeichert werden:

- Supabase Signed URLs,
- R2 Presigned URLs,
- Authorization-Header,
- R2-Zugangsdaten,
- kurzlebige URLs in Fehlertexten oder Telegram-Nachrichten.

Für Supabase lädt n8n das private Objekt mit seinem serverseitigen Credential
direkt. Für R2 gilt:

1. Ist eine stabile Custom-Domain-URL vorhanden, wird sie zuerst geprüft.
2. Schlägt nur dieser öffentliche Zugriff fehl, darf n8n dasselbe Objekt über
   den R2-S3-Endpunkt frisch signieren und erneut abrufen.
3. Fehlt eine Custom Domain, wird sofort eine neue Presigned URL aus Bucket und
   Objektpfad erzeugt.
4. Die erzeugte URL wird unmittelbar benutzt und weder in Postgres noch im
   Workflowzustand als dauerhafte Referenz behandelt.
5. Bei Wiederaufnahme wird immer neu signiert; eine möglicherweise abgelaufene
   URL aus einer alten Ausführung wird nicht wiederverwendet.

Der Wechsel von einer fehlerhaften Custom-Domain-URL auf einen frisch signierten
Abruf ist erlaubt, weil weiterhin exakt dasselbe vorbereitete Objekt geladen
wird. Der Wechsel auf ein neu generiertes Bild ist nicht erlaubt.

## 7. Supabase-Schema

Die Migration wird bei der Umsetzung mit
`supabase migration new prepared_carousel_images` erzeugt. Es wird kein
Migrationsdateiname erfunden und keine Migration im Rahmen der Planung remote
angewendet.

### 7.1 `carousel_source_media`

Neue Tabelle mit genau einer Zeile je ausdrücklich vorbereitetem Zitat oder
Kapitel:

```text
id                uuid primary key
scope             text not null ('quote' | 'chapter')
quote_id          uuid null references quotes(id) on delete restrict
chapter_id        uuid null references chapters(id) on delete restrict
storage_provider  text not null ('supabase' | 'cloudflare_r2')
storage_bucket    text not null
storage_path      text not null
public_url        text null
media_sha256      text not null
size_bytes        bigint not null
width             integer not null = 1080
height            integer not null = 1350
mime_type         text not null = 'image/jpeg'
created_at        timestamptz not null
updated_at        timestamptz not null
```

Die Tabelle enthält keine Kandidaten, Prompts, lokalen Dateipfade oder
Vorschaubilder. RLS wird aktiviert. `anon` und `authenticated` erhalten keine
Policies oder Rechte; ausschließlich der serverseitige Service-Zugriff darf die
Tabelle lesen und ändern.

Eine Check-Constraint verlangt exakt einen Besitzer:

- `scope = 'quote'`: `quote_id` gesetzt und `chapter_id` leer,
- `scope = 'chapter'`: `chapter_id` gesetzt und `quote_id` leer.

Partielle Unique-Indizes auf `quote_id` beziehungsweise `chapter_id` erzwingen
höchstens eine Zeile je Besitzer und dienen zugleich als Indizes für die beiden
Fremdschlüssel.

Constraints prüfen insbesondere:

- erlaubten Speicheranbieter,
- SHA-256-Format,
- positive und begrenzte Dateigröße,
- exakt 1080 x 1350 Pixel,
- MIME-Type `image/jpeg`,
- relative, traversal-freie Objektpfade,
- `public_url` nur als HTTPS-URL und nur optional.

### 7.2 Eingefrorener Post-Snapshot

`posts` erhält nullable Snapshotfelder für das vorbereitete Grundbild:

```text
source_image_provider
source_image_scope
source_image_owner_id
source_image_bucket
source_image_path
source_image_public_url
source_image_sha256
source_image_size_bytes
source_image_width
source_image_height
source_image_mime_type
error_code
```

Eine Tabellen-Constraint verlangt entweder einen vollständig leeren Snapshot
oder alle Pflichtfelder gemeinsam. `public_url` bleibt auch bei einem
vollständigen Snapshot optional.

`bookpromo_reserve` lädt innerhalb derselben kurzen Transaktion zuerst die
Zitatquelle und nur bei deren Abwesenheit die Kapitelquelle des gewählten
Zitats. Die gefundene Quelle wird atomar in den neuen `posts`-Datensatz kopiert.
Danach verändert ein späterer Bildwechsel den bereits reservierten Post nicht.

Die Semantik ist absichtlich streng:

- vorhandene Zitatquelle -> als `scope = 'quote'` einfrieren,
- keine Zitatquelle, aber vorhandene Kapitelquelle -> als `scope = 'chapter'`
  einfrieren,
- beide fehlen -> leerer Snapshot und Live-Generierung.

Ein späterer Abruffehler der eingefrorenen Zitatquelle fällt nicht auf die
Kapitelquelle zurück. Ein Abruffehler der Kapitelquelle fällt nicht auf FLUX
zurück. Nur echte Abwesenheit zum Reservierungszeitpunkt aktiviert die nächste
Stufe.

### 7.3 Schreib-RPC

Ein neuer `SECURITY INVOKER`-RPC verknüpft oder entfernt das eine Carouselbild
für einen expliziten `scope` und Besitzer. Er wird nur `service_role` gewährt
und prüft:

- Zitat beziehungsweise Kapitel und aktueller Buchstand existieren,
- ein Zitat ist aktuell, freigegeben, nicht gesperrt und nicht hochspoilernd,
- ein Kapitel ist aktuell und gehört zur aktuellen Buchversion,
- Payload und Pfad gehören zum erwarteten Buch/Zitat,
- beim Ersetzen oder Entfernen verweist kein offener Post auf den bisherigen
  Snapshot.

Das Sperren von Änderungen während eines offenen Posts verhindert, dass ein
bereits reservierter Lauf sein Quellobjekt mitten in der Verarbeitung verliert.
Ein partieller Index auf den Snapshot-Besitzer offener Posts hält diese Prüfung
auch bei wachsender Historie zielgerichtet. Alle beteiligten Besitzerzeilen
werden in stabiler Reihenfolge gesperrt; die Transaktion enthält keine
Storage-/HTTP-Aufrufe.

## 8. Upload- und Austauschreihenfolge

Bereitstellen eines neuen Bildes:

1. Lokale Quelle und Hash prüfen.
2. Normalisiertes JPEG rendern und Manifest prüfen.
3. Objekt unter Digestpfad hochladen.
4. Hochgeladenes Objekt testweise wieder lesen oder per HEAD/Manifest prüfen.
5. Datenbankverknüpfung über den RPC atomar setzen.
6. Lokale Quittung mit Scope, Zitat- oder Kapitel-ID, Anbieter, Pfad und Hash
   speichern.
7. UI erst danach als „für Carousel bereit“ anzeigen.

Beim Austausch wird zuerst das neue Objekt sicher bereitgestellt und erst dann
die Datenbankzeile ersetzt. Das alte Objekt darf anschließend nur gelöscht
werden, wenn der RPC bestätigt, dass kein offener Post darauf verweist. Ein
fehlgeschlagener Delete erzeugt höchstens ein verwaistes Objekt, niemals eine
kaputte aktive Verknüpfung; solche Objekte werden durch einen getrennten,
idempotenten Cleanup gefunden.

Beim Entfernen der Verknüpfung gilt dieselbe Sperre für offene Posts. Nach
erfolgreichem Entfernen verwendet ein künftig reservierter Post die nächste
vorhandene Prioritätsstufe: nach einem entfernten Zitatbild gegebenenfalls das
Kapitelbild, andernfalls die Live-Generierung. Bereits reservierte Posts behalten
ihren Snapshot.

## 9. Lokale Oberfläche

Die Zitat-Reel-Werkstatt und die Kapitelbild-/Kapitel-Teaser-Ansicht erhalten
beim ausgewählten Bild:

- eine 4:5-Vorschau des normalisierten Carousel-Grundbildes,
- Aktion „Für Carousel bereitstellen“,
- Status „nur lokal“, „wird übertragen“, „für Carousel bereit“ oder „veraltet“,
- Anbieter und Hash in einer technischen Detailansicht,
- Aktion „Carousel-Verknüpfung entfernen“, solange kein offener Post sie nutzt.

Die Aktion bleibt deaktiviert, wenn:

- kein Bild ausgewählt ist,
- das Bild als veraltet markiert ist,
- die Datei oder ihr Hash ungültig ist,
- Buch, Kapitel beziehungsweise Zitat noch nicht nach Supabase übertragen
  wurden,
- ein offener Post den aktuellen Bildsnapshot verwendet,
- Storage- oder Supabase-Konfiguration unvollständig ist.

Eine Änderung der lokal ausgewählten Bildvariante ändert die Remote-Verknüpfung
nicht automatisch. Dadurch bleibt die Veröffentlichungsauswahl ausdrücklich und
revisionssicher. Beide Ansichten zeigen zusätzlich, ob beim betreffenden Zitat
später dessen eigene Quelle oder die Kapitelquelle Vorrang hat.

## 10. n8n-Änderung

### 10.1 Verzweigung vor der Bildgenerierung

Der gemeinsame Carousel-Builder erhält nach `Image request` eine Bedingung:

- Snapshot vollständig: `Load prepared carousel image`,
- Snapshot leer: bestehender Node `Cloudflare FLUX image`.

Die Auswahl zwischen Zitat und Kapitel findet ausschließlich atomar in
`bookpromo_reserve` statt. n8n interpretiert keine veränderlichen
`carousel_source_media`-Zeilen nach und arbeitet nur mit dem eingefrorenen
Post-Snapshot.

Beide Zweige liefern dasselbe interne Binary- und JSON-Format an den gemeinsamen
Renderpfad. Das vorbereitete Bild ist bereits 1080 x 1350 und wird nicht erneut
beschnitten oder verzerrt. Es wird nur dekodiert und validiert. Der live erzeugte
Zweig behält die bisherige JPEG-Konvertierung, den Crop und das Resize.

### 10.2 Prüfung des vorbereiteten Bildes

Vor dem Hero-Render werden geprüft:

- HTTP-Antwort eindeutig erfolgreich,
- JPEG-Dateisignatur und dekodierbare Datei,
- maximal 8 MiB,
- exakt 1080 x 1350 Pixel,
- berechneter SHA-256 entspricht dem Post-Snapshot,
- keine Weiterleitung auf eine unerwartete Domain,
- Anbieter, Bucket und Objektpfad entsprechen dem Snapshot.

Erst nach dieser Prüfung wird dasselbe Binary für Hero- und Zitat-Slides
verwendet.

### 10.3 Resume und URL-Laufzeit

- Jeder Einstieg in den vorbereiteten R2-Zweig erzeugt bei Bedarf eine neue
  Presigned URL.
- Die Signatur erhält nur die für den unmittelbaren Download nötige Laufzeit.
- Eine Resume-Ausführung liest erneut den eingefrorenen Objektpfad und signiert
  neu.
- Eine Custom-Domain-URL gilt nur als Optimierung, nicht als einzige
  Wiederherstellungsmöglichkeit.
- Für die finalen privaten Carousel-Slides bleibt der bestehende Ablauf
  unverändert: Signed URLs werden unmittelbar vor Telegram/Meta neu erzeugt.

## 11. Fehler- und Telegram-Vertrag

Fehler im vorbereiteten Bildzweig werden mit
`error_code = 'prepared_image_unavailable'` oder einer engeren technischen
Unterkategorie gespeichert. Der Post wechselt kontrolliert zu `failed`.

Die Telegram-Nachricht enthält:

- klare Überschrift „Vorbereitetes Carouselbild nicht verwendbar“,
- Buch, Kapitel und einen gekürzten Zitatbeginn,
- Bildquelle „Zitat“ oder „Kapitel“,
- Post-ID,
- Speicheranbieter,
- verständliche Ursache wie „Objekt fehlt“, „Zugriff abgelehnt“,
  „JPEG beschädigt“, „falsche Abmessungen“ oder „Hash stimmt nicht“,
- Hinweis, dass kein Ersatzbild erzeugt und nichts veröffentlicht wurde,
- nächsten Schritt: Bild in der App erneut bereitstellen oder Verknüpfung
  bewusst entfernen und anschließend manuell neu starten.

Nicht enthalten sind Signed URLs, Authorization-Daten oder Storage-Secrets.
Review und Auto senden dieselbe eindeutige technische Meldung. Der Fehlerpfad
darf nicht in den vorhandenen automatischen „neues Zitat nach Bildmodellfehler“-
Loop gelangen.

Ein fehlgeschlagener Abruf über eine Custom Domain löst noch keine Meldung aus,
wenn der anschließende frisch signierte Abruf desselben R2-Objekts erfolgreich
ist. Erst wenn auch der autorisierte Objektabruf oder die Integritätsprüfung
scheitert, wird der Post gestoppt.

## 12. Reel-Vertrag bleibt unverändert

Die bestehende Reel-Queue behält:

- genau ein fertiges MP4 in `reel_assets`,
- Anbieter, Bucket, Objektpfad, optional stabile Custom-Domain-URL und
  MP4-Manifest,
- frische R2-/Supabase-Signed URLs beim Abruf, wenn keine stabile öffentliche
  URL vorhanden ist,
- Cleanup erst nach Abschluss aller Zielpublikationen.

Nicht in Supabase übernommen werden:

- `scene_image_path`,
- `optimized_image_path`,
- `uploaded_image_path`,
- `selected_image_path`,
- lokale Audioquellen oder Zwischenrender.

Gemeinsame Hilfsfunktionen für Anbieter, Pfadvalidierung und frische Signaturen
dürfen von Carousel und Reel wiederverwendet werden; ihre Datenmodelle bleiben
getrennt.

## 13. Automatisierte Tests

### 13.1 Python und lokale UI

- [ ] Nur die ausdrücklich ausgewählte Zitat- beziehungsweise Kapitelvariante
      wird hochgeladen.
- [ ] Andere lokale Kandidaten erscheinen weder im Upload noch im DB-Payload.
- [ ] Veraltete, fehlende oder hashabweichende Bilder werden abgelehnt.
- [ ] 4:5-Normalisierung ist deterministisch und erzeugt 1080 x 1350 JPEG/sRGB.
- [ ] Hoch-, Quer- und Reel-Hochformat zeigen vor Upload den exakten Ausschnitt.
- [ ] Identischer erneuter Upload ist idempotent.
- [ ] Bildaustausch ist bei offenem Post gesperrt.
- [ ] Supabase- und R2-Upload speichern keine Signed URL.
- [ ] R2 ohne Custom Domain speichert `public_url = NULL`.
- [ ] Reel-Enqueue überträgt weiterhin ausschließlich das MP4.
- [ ] Eine Kapitelquelle wird einmal gespeichert und nicht je Zitat dupliziert.

### 13.2 SQL

- [ ] Migration von Schema v10 auf die neue Version und Migration auf leerem
      Schema bestehen.
- [ ] RLS und Grants erlauben ausschließlich Service-Zugriff.
- [ ] Höchstens eine Medienzeile pro Zitat und pro Kapitel.
- [ ] Jede Medienzeile besitzt exakt einen Owner und die FK-Spalten sind durch
      die partiellen Unique-Indizes abgedeckt.
- [ ] Ungültige Provider, Pfade, Hashes, MIME-Typen und Dimensionen scheitern.
- [ ] `bookpromo_reserve` priorisiert Zitat vor Kapitel und friert einen
      vollständigen Bildsnapshot samt Scope und Owner ein.
- [ ] Reservierung ohne Medienzeile erzeugt einen vollständig leeren Snapshot.
- [ ] Fehlende Zitatquelle verwendet die Kapitelquelle.
- [ ] Eine vorhandene, später defekte Zitatquelle fällt nicht auf Kapitel oder
      FLUX zurück.
- [ ] Späterer Bildwechsel verändert einen vorhandenen Post nicht.
- [ ] Ersetzen/Entfernen bei offenem Post wird atomar abgelehnt.
- [ ] `fail` speichert den strukturierten Fehlercode revisionsgeschützt.
- [ ] Bestehende Carousel- und Reel-Verträge bleiben kompatibel.
- [ ] Datenbank-Advisors melden keine neuen Sicherheitsfehler.

### 13.3 n8n-Struktur- und Verhaltenstests

- [ ] Vorbereitetes Zitat- oder Kapitelbild überspringt den FLUX-Node
      vollständig.
- [ ] Fehlender Snapshot verwendet unverändert den bestehenden FLUX-Zweig.
- [ ] Supabase-Privatobjekt wird mit serverseitigem Credential geladen.
- [ ] R2 mit Custom Domain verwendet zunächst die stabile URL.
- [ ] Fehlerhafte Custom Domain fällt auf eine frisch signierte URL desselben
      Objekts zurück.
- [ ] R2 ohne Domain erzeugt bei jedem Lauf eine neue Presigned URL.
- [ ] Resume erzeugt eine neue URL und verwendet keine alte Signatur.
- [ ] 404, 403, Timeout, beschädigtes JPEG, falsche Maße und Hashabweichung
      stoppen den Post.
- [ ] In keinem dieser Fälle wird FLUX aufgerufen oder automatisch ein anderes
      Zitat reserviert.
- [ ] Genau eine spezifische Telegram-Meldung wird gesendet.
- [ ] Meldung und gespeicherte Ausführungsdaten enthalten keine Signed URL.
- [ ] Hero-, Zitat- und CTA-Manifest bleiben 3 bis 10 positionssortierte JPEGs.

## 14. Manueller Testplan

1. Buch und Zitat wie bisher nach Supabase übertragen.
2. Ein lokales Zitat-Reel-Bild auswählen und seine 4:5-Vorschau prüfen.
3. Zitatbild mit Supabase als Anbieter bereitstellen; DB-Zeile und privaten
   Objektpfad kontrollieren.
4. Review-Dry-Run starten und nachweisen, dass kein FLUX-Aufruf stattfand.
5. Telegram-Album und gerendertes Carousel visuell mit der lokalen Vorschau
   vergleichen.
6. Zitatverknüpfung entfernen, ein Kapitelbild bereitstellen und nachweisen,
   dass dieselbe Zitatreservierung nun die Kapitelquelle verwendet.
7. Auch die Kapitelverknüpfung entfernen und nachweisen, dass der Workflow nun
   wieder live generiert.
8. Ein eigenes Zitatbild zusätzlich zur Kapitelquelle bereitstellen und dessen
   Vorrang nachweisen.
9. R2 mit Custom Domain prüfen.
10. R2 ohne Custom Domain prüfen und nachweisen, dass `public_url` leer bleibt,
   aber der frisch signierte Abruf funktioniert.
11. Abgelaufene URL durch Resume simulieren; der neue Lauf muss neu signieren.
12. Custom Domain absichtlich unerreichbar machen; signierter Abruf desselben
    Objekts muss übernehmen.
13. Objekt entfernen beziehungsweise Hash verändern; der Lauf muss stoppen,
    Telegram informieren und darf weder FLUX noch Instagram aufrufen.
14. Einen offenen Post erzeugen und Bildaustausch in der App prüfen; die Aktion
    muss mit verständlichem Konflikt abgelehnt werden.
15. Einen Reel-Enqueue kontrollieren; nur das MP4 darf remote/DB-seitig neu
    erscheinen.

## 15. Implementierungsreihenfolge

### Phase 1: Lokaler Renderer und Speicherschnittstelle

- [ ] Reproduzierbaren 4:5-JPEG-Renderer aus ausgewählten Zitat- und
      Kapitelbildern bauen.
- [ ] Vorschau und lokale Validierung ergänzen.
- [ ] Bestehende Supabase-/R2-Clients um geprüften JPEG-Upload erweitern.
- [ ] Python-Tests abschließen.

**Fertig, wenn:** Die App exakt ein geprüftes Carousel-Grundbild erzeugen kann,
ohne es automatisch hochzuladen.

### Phase 2: Supabase-Vertrag

- [ ] Neue Migration über die Supabase CLI anlegen.
- [ ] `carousel_source_media`, Post-Snapshot, Constraints, Indizes, RLS und
      Grants ergänzen.
- [ ] Set-/Remove-RPC und erweiterte Reservierung implementieren.
- [ ] SQL- und Repository-Tests ausführen.
- [ ] Advisors prüfen und Findings beheben.

**Fertig, wenn:** Genau ein ausgewähltes Zitat- oder Kapitelbild revisionssicher
verknüpft und bei Reservierung mit der definierten Priorität unveränderlich in
den Post kopiert wird.

### Phase 3: App-Integration

- [ ] Bereitstellen-, Status- und Entfernen-Aktionen für Zitat und Kapitel
      umsetzen.
- [ ] Lokale Quittung und Konfliktbehandlung ergänzen.
- [ ] Austausch- und Orphan-Cleanup sicher implementieren.
- [ ] Keine automatische Kopplung an den allgemeinen Buch-Sync einführen.

**Fertig, wenn:** Nur eine ausdrückliche Benutzeraktion das jeweils eine
ausgewählte Zitat- oder Kapitelbild remote sichtbar macht.

### Phase 4: n8n-Hybridzweig

- [ ] Prepared-/Live-Verzweigung in den gemeinsamen Builder aufnehmen.
- [ ] Supabase-Download sowie R2-Public-/Signed-Abruf implementieren.
- [ ] Gemeinsame Integritätsprüfung anschließen.
- [ ] Spezifischen Fehlercode und Telegram-Pfad ergänzen.
- [ ] Review- und Auto-JSON reproduzierbar neu erzeugen.
- [ ] Strukturtests und lokale Workflowchecks ausführen.

**Fertig, wenn:** Ein vorbereiteter Snapshot ohne Bildmodell verarbeitet wird,
ein leerer Snapshot live generiert und ein defekter Snapshot sicher stoppt.

### Phase 5: Dokumentation und kontrollierte Aktivierung

- [ ] README, Supabase-Dokumentation und n8n-Anleitung aktualisieren.
- [ ] Migration zuerst lokal beziehungsweise in einer Testumgebung prüfen.
- [ ] Review-Dry-Run mit `publish_enabled=false` durchführen.
- [ ] Fehlerfälle und Telegram-Meldungen absichtlich testen.
- [ ] Erst danach Migration und neue Workflows kontrolliert produktiv ausrollen.
- [ ] Auto-Livetest erst nach bestandenem Review-Livetest.

## 16. Gesamtabnahme

Das Feature ist abgeschlossen, wenn:

- [ ] Supabase pro Zitat und Kapitel höchstens das eine ausdrücklich für
      Carousel gewählte Grundbild kennt.
- [ ] Kein weiterer lokaler Bildkandidat übertragen wird.
- [ ] Reel-Veröffentlichungen weiterhin ausschließlich finale MP4-Dateien
      einreihen.
- [ ] Die App vor Upload den exakten 4:5-Ausschnitt zeigt.
- [ ] Posts den Medienverweis bei Reservierung unveränderlich einfrieren.
- [ ] Die Auswahlreihenfolge Zitat -> Kapitel -> Live eindeutig eingehalten
      wird.
- [ ] Fehlende Medienverknüpfungen weiterhin live generiert werden.
- [ ] Defekte vorhandene Verknüpfungen niemals still live ersetzt werden.
- [ ] Telegram den Fehler verständlich und ohne geheime oder kurzlebige URL
      meldet.
- [ ] R2 ohne Custom Domain durch frisch erzeugte Presigned URLs funktioniert.
- [ ] Resume keine abgelaufene URL wiederverwendet.
- [ ] Custom-Domain-Ausfall auf denselben signierten R2-Gegenstand zurückfallen
      kann.
- [ ] Hash, JPEG und 1080-x-1350-Dimension vor jedem Render geprüft werden.
- [ ] Review- und Auto-Workflows aus demselben Builder-Code entstehen und alle
      bisherigen Publish-/Cleanup-Sicherungen behalten.

## 17. Relevante Primärdokumentation

- [Cloudflare R2: Presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
- [Cloudflare R2: öffentliche Buckets und Custom Domains](https://developers.cloudflare.com/r2/buckets/public-buckets/)
- [Supabase Storage: private Buckets und Signed URLs](https://supabase.com/docs/guides/storage/serving/downloads)
