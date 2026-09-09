# Supabase-Übertragung und Promotion-Vertrag

Stand: 08.09.2026. Das Projekt `AutomatedBookPromotion` (`aqfemzwrkzimzakqiwls`, PostgreSQL 17) wurde eindeutig mit der konfigurierten URL abgeglichen. Vor der Einrichtung war `public` leer. Angewendete und im Supabase-Migrationsverlauf registrierte Dateien:

1. `20260908065204_bookpromo_initial.sql`
2. `20260908065212_bookpromo_sync_and_approvals.sql`
3. `20260909012000_multiple_final_posts_per_day.sql`
4. `20260909093000_book_promotion_overlay_storage.sql`
5. `20260909100000_overlay_chapter_context.sql`
3. `20260908070640_bookpromo_transition_guards.sql`

Die Dateien liegen in `supabase/migrations`. Sie wurden mit der CLI angelegt; ihre Versionsnummern sind mit dem tatsächlich registrierten Verlauf abgeglichen. Die aktuelle API-Schemaversion ist **2**. Nicht erneut manuell das historische Bootstrap-Script ausführen.

## Lokale Daten übertragen

**Buchstand nach Supabase übertragen** sendet einen fertig analysierten, prüffreien Buchstand: Buchdaten und manuelles Profil, Buchversion und Dateihash, Kapiteltexte und Fundstellen, Originalzitate, Bewertungen sowie Sperren. Falls das Buch ein Titel-Overlay verwendet, erzeugt das Tool es im Format 1080×1350 (4:5), lädt das PNG in den privaten Storage-Bucket `book-promotion-assets` und speichert den Objektpfad als `profile.overlay_path`. Beim Reservieren ergänzt die Datenbank `chapter_position` und `chapter_name` in die gespeicherte Draft-Kopie des Buchprofils. Die DOCX, Zugangsdaten und lokale Schriftdateien werden nicht übertragen. Auch ein fertig analysierter Stand ohne geeignete Zitate ist übertragbar; daraus kann kein Entwurf reserviert werden.

Der Snapshot wird in einer lokalen Lesetransaktion erzeugt und auf 32 MiB begrenzt. Ein SHA-256-Hash identifiziert den gesamten Inhalt. Die serverseitige Funktion `bookpromo_sync` übernimmt alles in einer Postgres-Transaktion. Sie prüft Quellzuordnung, bestehende IDs und wortgetreue Ausschnitte erneut. Erst nach bestätigtem Erfolg wird eine lokale Quittung in `local_sync_receipts` gespeichert. Ein identischer erneuter Aufruf erzeugt keine doppelten Kapitel oder Zitate, auch wenn die vorherige Antwort verloren ging.

Eine abweichende entfernte Revision wird als Konflikt abgelehnt. Bücher mit offenen Entwürfen sind gegen gleichzeitige Übernahme geschützt. Alte Zitate/Kapitel werden bei verändertem aktuellem Snapshot ausgeblendet, nicht gelöscht; bestehende Post-Referenzen bleiben erhalten. Hat eine Quellversion bereits Posts, verlangt eine geänderte Extraktionsrevision den Import eines neuen Dokuments. Historischer Text wird nicht umgeschrieben.

Änderungen nach einer erfolgreichen Übertragung werden bewusst erst mit dem nächsten Klick übernommen. Die Übertragungsquittung ist keine automatische Synchronisation. Die Supabase-Buchübersicht und die lokale Verwaltung bleiben getrennt sichtbar. Die Kapitelseite liest letzte Veröffentlichung und Reservierung für bereits übertragene Zitate; fehlende/unerreichbare entfernte Datensätze werden nicht als „unbenutzt“ ausgegeben.

## RPC-Vertrag für n8n

Alle Aufrufe gehen an `POST /rest/v1/rpc/<name>` mit serverseitigem Credential. Funktionen laufen als `SECURITY INVOKER`; `anon` und `authenticated` dürfen sie nicht ausführen.

### `bookpromo_reserve`

Parameter: `p_account` (Zielkonto), `p_day` (lokales Datum `YYYY-MM-DD` in Europe/Berlin).

Ergebnis `outcome`: `inactive`, `no_quote`, `existing` oder `created`. Nur `created` startet im Workflow eine neue Generierung. `created`/`existing` enthalten `post` mit Entwurfs-ID, Status, Revision, technischem Aktionstoken, eingefrorenem Originalzitat und Buchprofil.

Die Auswahl sperrt die Konfiguration und serialisiert die kurze Reservierung auch kontenübergreifend. Buchauswahl ist gleichverteilt über geeignete aktive Bücher; danach wird innerhalb des Buchs ein verfügbares Zitat gewählt. Bereits veröffentlichte Zitate sind standardmäßig ausgeschlossen, verworfene Zitate unterliegen einer Sperrfrist. Ein offener Post blockiert das Konto und sein Zitat; der lokale Kalendertag ist zusätzlich eindeutig.

### `bookpromo_transition`

Parameter: `p_id` (UUID), `p_revision` (Integer), `p_token` (UUID), `p_action` (Text), `p_data` (JSON-Objekt). Revision und Token müssen beide exakt dem aktuellen gespeicherten Stand entsprechen, auch NULL wird abgelehnt. Erfolgreiche Aktionen rotieren Revision und Token. Ergebnis: `updated` mit `post`, `stale` oder `invalid_state`.

| Aktion | Erwarteter Status → Ergebnis | Zusätzliche Daten |
|---|---|---|
| `text_ready` | generating_text → awaiting_text_approval | caption, image_prompt; Caption maximal 2200 Zeichen und enthält das Originalzitat |
| `approve_text` | awaiting_text_approval → generating_image | chat_id, user_id |
| `image_ready` | generating_image → awaiting_image_approval | image_url (HTTPS); Textfreigabe muss aktuell sein |
| `approve_image` | awaiting_image_approval → approved | chat_id, user_id |
| `retry_text` | eine Freigabephase → generating_text | chat_id, user_id; bisherige Text-/Bildfreigaben verfallen |
| `retry_image` | awaiting_image_approval → generating_image | chat_id, user_id; Textfreigabe bleibt erhalten |
| `discard` | eine Freigabephase → discarded | chat_id, user_id |
| `begin_publish` | approved → publishing | keine |
| `container_ready` | publishing → publishing | container_id; nur einmalig |
| `publish_uncertain` | publishing → publish_uncertain | keine |
| `published` | publishing/publish_uncertain → published | media_id, optional permalink; Container muss gespeichert sein |
| `fail` | generating_text/generating_image/approved → failed | keine |

Telegram-Aktionen vergleichen Chat **und** Benutzer mit `promotion_settings`. Die vom Bot beobachtete Identität wird weitergegeben; es gibt kein öffentliches Freigabe-RPC. Service-Credentials sind privilegiert und bleiben ausschließlich bei den eigenen Backends. Die Freigabesicherung setzt voraus, dass die Credentials nicht an unberechtigte Personen ausgegeben werden.

## Verifikation und Betriebsstand

Geprüft: Python-Tests für atomare Übertragung, unveränderte Wiederholung nach unklarer Antwort, Ursprungsschutz, Quittungen und echte/fehlende Veröffentlichungshistorie; lokale PostgreSQL-Tests für Schema, Berechtigungen, Quellprüfung, Reservierung, getrennte Freigaben und alte/fehlende Tokens. Im Zielprojekt wurden Data-API-Lesezugriff, RLS/Grants und ein temporärer Schreib-/Lese-Test mit `ROLLBACK` geprüft. Es wurden keine Testbücher dauerhaft angelegt.

Supabase Security Advisors melden ausschließlich **INFO** für RLS ohne öffentliche Policies. Das entspricht dem beabsichtigten Backend-Zugriff: keine Buchtexte für `anon`/`authenticated`. Erklärung: [RLS ohne Policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy). Die Performance-Hinweise betreffen unbenutzte Indizes in der noch leeren Datenbank und den zusammengesetzten Fremdschlüssel von `books`; dessen führende Buch-ID ist bereits eindeutig über den Primärschlüssel indiziert.

Das vorbereitete Zielkonto ist inaktiv. Das bisher vorhandene lokale Buch war noch nicht analysiert, daher wurde noch kein Nutzerbuch nach Supabase übernommen. Der n8n-Export liegt bereit; Credential-Zuordnung, erlaubter Telegram-Nutzer, Medienaufbewahrung und der kontrollierte Gesamttest sind die nächsten Einrichtungsschritte. Siehe [n8n-Anleitung](../n8n/README.md).
