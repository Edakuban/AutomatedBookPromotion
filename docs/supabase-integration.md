# Supabase-Übertragung und Promotion-Vertrag

Stand: 02.10.2026. Der lokale Code unterstützt die Verträge v8–v10. Das produktive Projekt `AutomatedBookPromotion` (`aqfemzwrkzimzakqiwls`, PostgreSQL 17) wurde nach ausdrücklicher Freigabe auf v10 migriert. Migrationen in Namensreihenfolge:

1. `20260908065204_bookpromo_initial.sql`
2. `20260908065212_bookpromo_sync_and_approvals.sql`
3. `20260908070640_bookpromo_transition_guards.sql`
4. `20260909012000_multiple_final_posts_per_day.sql`
5. `20260909093000_book_promotion_overlay_storage.sql`
6. `20260909100000_overlay_chapter_context.sql`
7. `20260909114500_allow_book_sync_with_open_drafts.sql`
8. `20260910050339_carousel_contract_and_media_storage.sql`
9. `20260926090000_image_quote_retry.sql`
10. `20260926170000_reel_publication_queue.sql`
11. `20260927120000_reel_multiplatform_storage.sql`
12. `20260927130000_reel_claim_all_accounts.sql`
13. `20260930090000_chapter_reel_sources.sql`
14. `20261002080336_book_teaser_sources.sql`

Die Dateien liegen in `supabase/migrations`. Die höchste Python-Schemaversion ist **10**. v8 bleibt für vorhandene Zitat-/Carousel-Abläufe nutzbar; die Kapitel-Queue verlangt v9, die Gesamt-Teaser-Queue v10. Beide neuen Migrationen wurden am 02.10.2026 remote angewendet. Verifiziert: Schemaversion 10 über SQL und den vollständigen Python-Data-API-Schemacheck, unveränderter Buchbestand (11 Bücher, 185 Kapitel, 1646 Zitate), RLS, ausschließlich Service-Role-Zugriff auf den Enqueue-RPC und privater 300-MiB-MP4-Bucket. Es wurden keine Veröffentlichungen oder Uploads ausgelöst. Nicht manuell das historische Bootstrap-Script ausführen.

## Fertige Reel-Warteschlange

`reel_assets` speichert ausschließlich lokal geprüfte, vollständige Reels samt Quelltyp und Zitat- oder Kapitelreferenz, belegtem Quelltext, Titel, Beschreibung, Buchprofil, Prompts, Storage-Anbieter und geprüftem MP4-Manifest. Kapitel-Reels dürfen bis zu 60 Sekunden lang sein. `reel_publications` enthält je gewählter Plattform einen unveränderlichen Snapshot aus Konto, Daily-/Terminmodus und Plattformoptionen. `bookpromo_reel_enqueue` schreibt Asset und ein bis vier Ziele atomar. `bookpromo_reel_claim` trennt tägliche FIFO- und fällige Terminläufe; `bookpromo_reel_transition` sichert externe IDs, unklare und bestätigte Zustände mit Revision und Aktionstoken. Erst wenn alle Ziele `published` oder `cancelled` sind, wechselt das Asset zu `cleanup_pending`; `bookpromo_reel_cleanup` quittiert die bereits extern erfolgreiche Löschung. Der Publisher erzeugt weder Text noch Medien und liest keine veränderlichen Buchdaten nach.

## Gesamt-Teaservideos in der Veröffentlichungsqueue

Nach v10 kann dieselbe Queue ein geprüftes Gesamt-Teaservideo mit `source_kind=book_teaser`, Buchreferenz und NULL für Zitat-/Kapitelreferenz enthalten. MP4-Hash, tatsächliche Dimensionen, Audio und Dauer werden lokal vor dem Upload geprüft. Die Asset-ID ist für dieselbe Projektrevision und denselben Video-Hash deterministisch; explizites erneutes Einreihen erstellt eine neue ID. Der bisherige `quote_text`-Pflichtwert enthält hier einen Titel-Marker, kein erfundenes Buchzitat; n8n verlangt für diesen Quelltyp keine Zitatwiederholung in der Beschreibung.

Gesamt-Teaser erlauben 4–600 Sekunden und bis 300 MiB; das lokale konfigurierbare Upload-Limit beträgt standardmäßig 256 MiB. Zitat-/Kapitel-Assets behalten ihr 50-MiB- und 60-Sekunden-Limit. Der private MP4-Bucket wird durch v10 auf 300 MiB erweitert, ohne öffentliche Policies oder neue Grants. Bestehende Invoker-RPCs, atomare Ziel-Snapshots und Cleanup-Regeln bleiben erhalten.

Aktuell angeboten: YouTube für 16:9 und 9:16, Instagram nur für Hochformat. Facebook und TikTok für Gesamt-Teaser sind noch nicht unterstützt; ihre n8n-Zweige bleiben deaktiviert. YouTube verwendet den normalen Videos-Upload ohne hinzugefügtes `#Shorts`. Die Plattform selbst klassifiziert Hochformat bis einschließlich 180 Sekunden als Shorts; ein normaler Langvideo-Upload ist deshalb mit 16:9 eindeutig. Quellen: [YouTube-Klassifizierung](https://support.google.com/youtube/answer/15424877), [Meta-Reels-Spezifikationen](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md).

Daily-/Terminmodus und Plattformoptionen werden wie bei Kapitel-Reels eingefroren. Den aktualisierten `book-promotion-reel-publisher.json` separat in n8n importieren und zunächst mit `publish_enabled=false` prüfen. Es wurden weder Live-Uploads noch Veröffentlichungen oder Remote-Migrationen durch diesen Implementierungsschritt ausgeführt.

## Lokale Daten übertragen

**Buchstand nach Supabase übertragen** sendet einen fertig analysierten, prüffreien Buchstand: Buchdaten und manuelles Profil, Buchversion und Dateihash, Kapiteltexte und Fundstellen, Originalzitate, Bewertungen sowie Sperren. Vor dem Netzwerkzugriff friert das Tool den lokalen Einstellungs- und Assetstand in einer revisionsgeschützten SQLite-Transaktion ein und rendert daraus Titel-Overlay und CTA-Schlussseite erneut. Das PNG-Overlay wird unter `<book_id>/<sha256>.png`, das CTA-JPEG unter `<book_id>/carousel/<sha256>.jpg` im privaten Bucket `book-promotion-assets` gespeichert. `profile` enthält anschließend `overlay_path`, `publication_mode`, `carousel_end_text` und `carousel_end_slide_path`. Beim Reservieren ergänzt die Datenbank `chapter_position` und `chapter_name` in die unveränderliche Draft-Kopie des Buchprofils. Frontcover, Logo, DOCX, Zugangsdaten und lokale Schriftdateien werden nicht übertragen. Auch ein inaktiver, noch unvollständiger Buchstand oder ein Stand ohne geeignete Zitate ist übertragbar; daraus kann kein Entwurf reserviert werden.

Vor dem Storage-Upload prüft Python den kompatiblen Schema-Vertrag (v8–v10); gegen inkompatible Stände wird mit einer verständlichen Migrationsmeldung abgebrochen. Ein SHA-256-Hash identifiziert den gesamten Payload einschließlich der endgültigen privaten Objektpfade. Die serverseitige Funktion `bookpromo_sync` übernimmt alles in einer Postgres-Transaktion. Sie prüft Quellzuordnung, bestehende IDs und wortgetreue Ausschnitte erneut. Erst nach bestätigtem Erfolg wird eine lokale Quittung in `local_sync_receipts` gespeichert. Ein identischer erneuter Aufruf erzeugt keine doppelten Kapitel oder Zitate, auch wenn die vorherige Antwort verloren ging. Digestpfade machen wiederholte Asset-Uploads inhaltlich identisch.

Eine abweichende entfernte Revision wird als Konflikt abgelehnt. Ein offener Entwurf behält sein eingefrorenes `quote_text` und `book_profile`; spätere Buch-Synchronisationen verändern diesen Snapshot nicht. Alte Zitate/Kapitel werden bei verändertem aktuellem Snapshot ausgeblendet, nicht gelöscht; bestehende Post-Referenzen bleiben erhalten. Hat eine Quellversion bereits Posts, verlangt eine geänderte Extraktionsrevision den Import eines neuen Dokuments. Historischer Text wird nicht umgeschrieben.

Änderungen nach einer erfolgreichen Übertragung werden bewusst erst mit dem nächsten Klick übernommen. Die Übertragungsquittung ist keine automatische Synchronisation. Die Supabase-Buchübersicht und die lokale Verwaltung bleiben getrennt sichtbar. Die Kapitelseite liest letzte Veröffentlichung und Reservierung für bereits übertragene Zitate; fehlende/unerreichbare entfernte Datensätze werden nicht als „unbenutzt“ ausgegeben.

## RPC-Vertrag für n8n

Alle Aufrufe gehen an `POST /rest/v1/rpc/<name>` mit serverseitigem Credential. Funktionen laufen als `SECURITY INVOKER`; `anon` und `authenticated` dürfen sie nicht ausführen.

### `bookpromo_reserve`

Parameter: `p_account` (Zielkonto), `p_day` (lokales Datum `YYYY-MM-DD` in Europe/Berlin), `p_execution_mode` (`review` oder `auto`) und für neue Workflows `p_generation_attempt` (0–10). Der kompatible Drei-Parameter-Aufruf setzt den Versuch auf 0.

Ergebnis `outcome`: `inactive`, `no_quote`, `existing`, `created` oder `blocked_by_other_mode`. Nur `created` startet eine neue Generierung. `created`/`existing` enthalten `post` mit Entwurfs-ID, Modus, Status, Revision, technischem Aktionstoken, eingefrorenem Originalzitat und Buchprofil. Ein offener Entwurf wird nur vom Workflow desselben Modus wiederaufgenommen.

Die Auswahl sperrt die Konfiguration und serialisiert die kurze Reservierung auch kontenübergreifend. Sie berücksichtigt nur aktive, vollständig konfigurierte Bücher mit passendem `publication_mode`. Buchauswahl ist gleichverteilt über geeignete Bücher; danach wird innerhalb des Buchs ein verfügbares Zitat gewählt. Bereits veröffentlichte Zitate sind standardmäßig ausgeschlossen, verworfene Zitate unterliegen einer Sperrfrist. Fehlgeschlagene Zitate werden für denselben Modus am selben Tag ausgeschlossen. Ein offener Post blockiert das Konto und sein Zitat. Der übergebene Generierungsversuch wird in `posts.attempts` gespeichert.

### `bookpromo_transition`

Parameter: `p_id` (UUID), `p_revision` (Integer), `p_token` (UUID), `p_action` (Text), `p_data` (JSON-Objekt). Revision und Token müssen beide exakt dem aktuellen gespeicherten Stand entsprechen, auch NULL wird abgelehnt. Erfolgreiche Aktionen rotieren Revision und Token. Ergebnis: `updated` mit `post`, `stale` oder `invalid_state`.

| Aktion | Erwarteter Status → Ergebnis | Zusätzliche Daten |
|---|---|---|
| `text_ready` | generating_text → Review: awaiting_text_approval; Auto: generating_image | caption, image_prompt; Auto protokolliert `system:auto` |
| `approve_text` | awaiting_text_approval → generating_image | chat_id, user_id |
| `media_ready` | generating_image → Review: awaiting_image_approval; Auto: approved | vollständiges Array aus 3–10 privaten Medienpfaden, Digests, Positionen und Typen |
| `approve_image` | awaiting_image_approval → approved | chat_id, user_id |
| `retry_text` | eine Review-Freigabephase → generating_text | chat_id, user_id; bisherige Medien werden `cleanup_pending` |
| `retry_image` | awaiting_image_approval → generating_image | chat_id, user_id; Textfreigabe bleibt erhalten, Medien werden `cleanup_pending` |
| `discard` | eine Review-Freigabephase → discarded | chat_id, user_id; vorhandene Medien werden `cleanup_pending` |
| `begin_publish` | approved → publishing | nur bei vollständigem hochgeladenem Manifest |
| `carousel_container_ready` | publishing → publishing | Parent-ID; alle Child-Container müssen bereit sein |
| `publish_uncertain` | publishing → publish_uncertain | optionaler Fehler; alle IDs und Medien bleiben erhalten |
| `published` | publishing/publish_uncertain → published | media_id, optional permalink; danach werden Medien `cleanup_pending` |
| `fail` | generating_text/generating_image/approved → failed | optionaler Fehler; vorhandene Medien werden `cleanup_pending` |

Telegram-Aktionen vergleichen Chat **und** Benutzer mit `promotion_settings`. Die vom Bot beobachtete Identität wird weitergegeben; es gibt kein öffentliches Freigabe-RPC. Service-Credentials sind privilegiert und bleiben ausschließlich bei den eigenen Backends. Die Freigabesicherung setzt voraus, dass die Credentials nicht an unberechtigte Personen ausgegeben werden.

### `bookpromo_media_container`

Speichert eine Child-Container-ID für genau eine Manifestposition. Parameter: Post-ID, aktuelle Revision, aktueller Aktionstoken, Position und Container-ID. Derselbe erneute Aufruf ist idempotent; eine abweichende zweite ID wird als Konflikt abgelehnt. Der RPC verändert die Postrevision nicht, sodass mehrere positionssortierte Child-Aufrufe denselben freigegebenen Publish-Stand verwenden können.

### `bookpromo_media_cleanup`

Bestätigt nach einer erfolgreichen Storage-Löschung die betroffenen privaten `storage_path`-Werte. Nur `cleanup_pending`-Medien des revisions- und tokengebundenen Posts dürfen zu `deleted` wechseln. Teilbereinigungen liefern `pending`, vollständige oder identisch wiederholte Aufrufe `complete`. Der RPC löscht keine Storage-Objekte selbst und kann deshalb keinen externen Netzwerkaufruf innerhalb einer Datenbanktransaktion auslösen.

## Temporäre Carousel-Medien

`post_media` speichert 3–10 geordnete Elemente mit `kind=hero|quote|cta`, SHA-256, privatem Objektpfad und Child-Container-ID. Das Manifest verlangt exakt einen Hero an Position 0, mindestens einen Zitat-Slide und genau einen CTA am Ende. Objektpfade sind an Post-ID, Manifestrevision, Position und Digest gebunden.

Der private Bucket `book-promotion-media` akzeptiert ausschließlich JPEGs bis 8 MiB. Der ebenfalls private dauerhafte Bucket `book-promotion-assets` akzeptiert PNG und JPEG bis 8 MiB. Es gibt keine öffentlichen Storage-Policies; Upload, Signed URLs und Löschung erfolgen über das serverseitige Supabase-Credential. Signed URLs selbst werden nicht persistiert. Nach `media_publish` wird zuerst `published` gespeichert, danach löscht n8n die Objekte und bestätigt den Cleanup. Bei `publish_uncertain` bleiben Pfade und Container-IDs vollständig erhalten.

## Verifikation und Betriebsstand

Lokal geprüft: Migration des leeren Schemas und eines v4-Testbestands, RLS/Grants, Review-/Auto-Reservierung, vollständige Manifestvalidierung, Retries, idempotente und widersprüchliche Child-Container, Parent-/Publish-Zustände sowie vollständiger und teilweiser Cleanup. Live geprüft: Schemaversion, Datenbestand, Tabellen und Spalten, RLS, Funktionsrechte, Storage-Buckets sowie Python-Lesezugriff. Die drei bisherigen Test-Posts wurden vom Migrationsguard entfernt; Buch, 25 Kapitel und 260 Zitate blieben erhalten.

Supabase Security Advisors melden ausschließlich **INFO** für RLS ohne öffentliche Policies. Das entspricht dem beabsichtigten Backend-Zugriff: keine Buchtexte für `anon`/`authenticated`. Erklärung: [RLS ohne Policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy). Die Performance-Hinweise betreffen unbenutzte Indizes in der noch leeren Datenbank und den zusammengesetzten Fremdschlüssel von `books`; dessen führende Buch-ID ist bereits eindeutig über den Primärschlüssel indiziert.

Der bestehende Einzelbild-Workflow ist mit dem aktuellen Schema bewusst nicht mehr kompatibel und wurde entfernt. Die beiden lokal generierten Carousel-Workflows verwenden den hier beschriebenen RPC- und Storage-Vertrag; Migration, Import, Credential-Zuordnung, Dry-Run und Livetest erfolgen bewusst separat. Siehe [n8n-Anleitung](../n8n/README.md).
