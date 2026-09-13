# Supabase-Schema und Einrichtung

Stand: 10.09.2026. Im konfigurierten Projekt `AutomatedBookPromotion` läuft Schema v5 für Carousels. Die Migration und der Python-Lesezugriff wurden live geprüft. Promotion-Einstellung und Buch sind weiterhin aktiv, aber das noch nicht erneut synchronisierte v4-Buchprofil wird von der v5-Reservierung nicht ausgewählt.

## Maßgebliche Migrationen

Die Dateien in `supabase/migrations` sind der aktuelle, mit dem entfernten Migrationsverlauf abgeglichene Stand:

1. `20260908065204_bookpromo_initial.sql`
2. `20260908065212_bookpromo_sync_and_approvals.sql`
3. `20260908070640_bookpromo_transition_guards.sql`
4. `20260909012000_multiple_final_posts_per_day.sql`
5. `20260909093000_book_promotion_overlay_storage.sql`
6. `20260909100000_overlay_chapter_context.sql`
7. `20260909114500_allow_book_sync_with_open_drafts.sql`
8. `20260910050339_carousel_contract_and_media_storage.sql`

Auf dem bestehenden Projekt frühere Migrationen nicht erneut manuell anwenden. Der passende Python-Sync ist bereits auf v5 umgestellt; nach Anwendung der v5-Migration bleibt die Promotion deaktiviert, bis die neuen n8n-Workflows bereitstehen. Für ein neues, leeres Projekt alle Dateien in Namensreihenfolge über die Supabase-Migrationsverwaltung anwenden. `sql/001_initial_schema.sql` ist das historische Bootstrap-Script für v1 und allein nicht ausreichend. `sql/002_verify_setup.sql` enthält lesende Prüfungen des v5-Vertrags.

Die Migrationen legen neun Anwendungstabellen, drei Übersichts-Views und fünf serverseitige RPCs an. Alle Anwendungstabellen haben RLS ohne öffentliche Policies; Views und Funktionen verwenden die Rechte des aufrufenden Servers. `anon` und `authenticated` erhalten keinen Zugriff auf Buchtexte, Medienmanifeste oder Promotion-RPCs. Der private temporäre Bucket `book-promotion-media` akzeptiert JPEGs bis 8 MiB; `book-promotion-assets` akzeptiert dauerhafte PNG-Overlays und CTA-JPEGs bis 8 MiB. Der Service-Key bleibt im Python-Backend beziehungsweise in n8n-Credentials.

## Verbindung und Datenübertragung

In der privaten `.env` müssen `SUPABASE_URL`, `SUPABASE_SECRET_KEY` und `SUPABASE_ENABLED=true` gesetzt sein. Nach Änderungen die Anwendung neu starten. Verbindung prüfen:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m bookpromo check-db
```

Ein fertiger, prüffreier Analysestand lässt sich auf der Buchseite mit **Buchstand nach Supabase übertragen** übernehmen. Das übernimmt Kapiteltexte, Originalzitate, Bewertungen und manuelle Einstellungen; Originaldateien bleiben lokal. Spätere Änderungen verlangen einen erneuten Klick. Bereits verwendete Quellen und offene Entwürfe sind gegen widersprüchliche Übernahmen geschützt.

Vertragsdetails, angewendete Prüfungen und aktueller Datenstand: [Supabase-Integration](../docs/supabase-integration.md). Workflow-Import und Freigaben: [n8n-Anleitung](../n8n/README.md).

## Lokale Prüfungen

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest
node tools/check_schema.mjs .uv-cache/pglite-0.5.8/package/dist/index.js
node tools/check_integration.mjs .uv-cache/pglite-0.5.8/package/dist/index.js
```

Die SQL-Prüfungen benötigen Node.js und PGlite 0.5.8; der angegebene Cachepfad ist in dieser Entwicklungsumgebung vorhanden. Auf einem anderen Rechner den Pfad zum installierten PGlite-Modul übergeben. Tests laufen ausschließlich im Speicher mit simulierten Supabase-Rollen. Geprüft werden Constraints, Grants, RLS, Quelltreue, Review-/Auto-Reservierungen, atomare Medienmanifeste, idempotente Container-IDs, unklare Veröffentlichungen sowie der revisionsgeschützte Cleanup.

Im Zielprojekt wurden außerdem Data-API-Lesezugriff und ein temporärer Schreib-/Lese-Test mit Rollback geprüft. Die Sicherheitsprüfung meldet lediglich erwartete INFO-Hinweise für RLS ohne öffentliche Policies.

## Offizielle Grundlagen

- [Supabase: API-Zugriff und Grants](https://supabase.com/docs/guides/api/securing-your-api)
- [Supabase: API-Keys](https://supabase.com/docs/guides/getting-started/api-keys)
- [Supabase: RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [PGlite: lokale PostgreSQL-Tests](https://pglite.dev/docs/)
