# Supabase-Übertragung und Promotion-Vertrag

Stand: 10.09.2026. Das produktive Projekt `AutomatedBookPromotion` (`aqfemzwrkzimzakqiwls`, PostgreSQL 17) läuft mit dem Carousel-Vertrag v5. Migrationen in Namensreihenfolge:

1. `20260908065204_bookpromo_initial.sql`
2. `20260908065212_bookpromo_sync_and_approvals.sql`
3. `20260908070640_bookpromo_transition_guards.sql`
4. `20260909012000_multiple_final_posts_per_day.sql`
5. `20260909093000_book_promotion_overlay_storage.sql`
6. `20260909100000_overlay_chapter_context.sql`
7. `20260909114500_allow_book_sync_with_open_drafts.sql`
8. `20260910050339_carousel_contract_and_media_storage.sql`

Die Dateien liegen in `supabase/migrations`. Die v5-Datei wurde mit der Supabase-CLI angelegt und am 10.09.2026 auf das Zielprojekt angewendet. Ziel- und Python-Schemaversion sind **5**. Nicht manuell das historische Bootstrap-Script ausführen.

## Lokale Daten übertragen

**Buchstand nach Supabase übertragen** sendet einen fertig analysierten, prüffreien Buchstand: Buchdaten und manuelles Profil, Buchversion und Dateihash, Kapiteltexte und Fundstellen, Originalzitate, Bewertungen sowie Sperren. Vor dem Netzwerkzugriff friert das Tool den lokalen Einstellungs- und Assetstand in einer revisionsgeschützten SQLite-Transaktion ein und rendert daraus Titel-Overlay und CTA-Schlussseite erneut. Das PNG-Overlay wird unter `<book_id>/<sha256>.png`, das CTA-JPEG unter `<book_id>/carousel/<sha256>.jpg` im privaten Bucket `book-promotion-assets` gespeichert. `profile` enthält anschließend `overlay_path`, `publication_mode`, `carousel_end_text` und `carousel_end_slide_path`. Beim Reservieren ergänzt die Datenbank `chapter_position` und `chapter_name` in die unveränderliche Draft-Kopie des Buchprofils. Frontcover, Logo, DOCX, Zugangsdaten und lokale Schriftdateien werden nicht übertragen. Auch ein inaktiver, noch unvollständiger Buchstand oder ein Stand ohne geeignete Zitate ist übertragbar; daraus kann kein Entwurf reserviert werden.

Vor dem Storage-Upload prüft Python ausdrücklich Schemaversion 5; gegen v4 wird mit einer verständlichen Migrationsmeldung abgebrochen. Ein SHA-256-Hash identifiziert den gesamten Payload einschließlich der endgültigen privaten Objektpfade. Die serverseitige Funktion `bookpromo_sync` übernimmt alles in einer Postgres-Transaktion. Sie prüft Quellzuordnung, bestehende IDs und wortgetreue Ausschnitte erneut. Erst nach bestätigtem Erfolg wird eine lokale Quittung in `local_sync_receipts` gespeichert. Ein identischer erneuter Aufruf erzeugt keine doppelten Kapitel oder Zitate, auch wenn die vorherige Antwort verloren ging. Digestpfade machen wiederholte Asset-Uploads inhaltlich identisch.

Eine abweichende entfernte Revision wird als Konflikt abgelehnt. Ein offener Entwurf behält sein eingefrorenes `quote_text` und `book_profile`; spätere Buch-Synchronisationen verändern diesen Snapshot nicht. Alte Zitate/Kapitel werden bei verändertem aktuellem Snapshot ausgeblendet, nicht gelöscht; bestehende Post-Referenzen bleiben erhalten. Hat eine Quellversion bereits Posts, verlangt eine geänderte Extraktionsrevision den Import eines neuen Dokuments. Historischer Text wird nicht umgeschrieben.

Änderungen nach einer erfolgreichen Übertragung werden bewusst erst mit dem nächsten Klick übernommen. Die Übertragungsquittung ist keine automatische Synchronisation. Die Supabase-Buchübersicht und die lokale Verwaltung bleiben getrennt sichtbar. Die Kapitelseite liest letzte Veröffentlichung und Reservierung für bereits übertragene Zitate; fehlende/unerreichbare entfernte Datensätze werden nicht als „unbenutzt“ ausgegeben.

## RPC-Vertrag für n8n

Alle Aufrufe gehen an `POST /rest/v1/rpc/<name>` mit serverseitigem Credential. Funktionen laufen als `SECURITY INVOKER`; `anon` und `authenticated` dürfen sie nicht ausführen.

### `bookpromo_reserve`

Parameter: `p_account` (Zielkonto), `p_day` (lokales Datum `YYYY-MM-DD` in Europe/Berlin), `p_execution_mode` (`review` oder `auto`).

Ergebnis `outcome`: `inactive`, `no_quote`, `existing`, `created` oder `blocked_by_other_mode`. Nur `created` startet eine neue Generierung. `created`/`existing` enthalten `post` mit Entwurfs-ID, Modus, Status, Revision, technischem Aktionstoken, eingefrorenem Originalzitat und Buchprofil. Ein offener Entwurf wird nur vom Workflow desselben Modus wiederaufgenommen.

Die Auswahl sperrt die Konfiguration und serialisiert die kurze Reservierung auch kontenübergreifend. Sie berücksichtigt nur aktive, vollständig konfigurierte Bücher mit passendem `publication_mode`. Buchauswahl ist gleichverteilt über geeignete Bücher; danach wird innerhalb des Buchs ein verfügbares Zitat gewählt. Bereits veröffentlichte Zitate sind standardmäßig ausgeschlossen, verworfene Zitate unterliegen einer Sperrfrist. Ein offener Post blockiert das Konto und sein Zitat.

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

Der bestehende Einzelbild-Workflow ist mit Live-Schema v5 bewusst nicht mehr kompatibel und wurde entfernt. Die beiden lokal generierten Carousel-Workflows verwenden den hier beschriebenen RPC- und Storage-Vertrag; Import, Credential-Zuordnung, Dry-Run und Livetest erfolgen bewusst separat. Siehe [n8n-Anleitung](../n8n/README.md).
