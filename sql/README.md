# Supabase-Schema und Einrichtung

Stand: 08.09.2026. **Schritt 3.2 ist abgeschlossen.** Im konfigurierten Projekt `AutomatedBookPromotion` ist Schema v2 eingerichtet; die lokale Verbindung ist aktiviert und geprüft. Die Promotion-Konfiguration bleibt inaktiv.

## Maßgebliche Migrationen

Die Dateien in `supabase/migrations` sind der aktuelle, mit dem entfernten Migrationsverlauf abgeglichene Stand:

1. `20260908065204_bookpromo_initial.sql`
2. `20260908065212_bookpromo_sync_and_approvals.sql`
3. `20260908070640_bookpromo_transition_guards.sql`

Auf dem bestehenden Projekt diese Migrationen nicht erneut manuell anwenden. Für ein neues, leeres Projekt alle drei in dieser Reihenfolge über die Supabase-Migrationsverwaltung anwenden. `sql/001_initial_schema.sql` ist das historische Bootstrap-Script für v1 und allein nicht ausreichend für die aktuelle Anwendung. `sql/002_verify_setup.sql` enthält lesende Prüfungen des aktuellen Vertrags.

Die Migrationen legen acht Tabellen, drei Übersichts-Views und drei serverseitige RPCs an. Alle Tabellen haben RLS ohne öffentliche Policies; Views und Funktionen verwenden die Rechte des aufrufenden Servers. `anon` und `authenticated` erhalten keinen Zugriff auf Buchtexte oder Promotion-RPCs. Der Service-Key bleibt im Python-Backend beziehungsweise in n8n-Credentials.

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

Die SQL-Prüfungen benötigen Node.js und PGlite 0.5.8; der angegebene Cachepfad ist in dieser Entwicklungsumgebung vorhanden. Auf einem anderen Rechner den Pfad zum installierten PGlite-Modul übergeben. Tests laufen ausschließlich im Speicher mit simulierten Supabase-Rollen. Geprüft werden Constraints, Grants, RLS, Quelltreue, atomare Übernahme, Reservierungen, getrennte Freigaben sowie alte oder fehlende Revisionen und Tokens.

Im Zielprojekt wurden außerdem Data-API-Lesezugriff und ein temporärer Schreib-/Lese-Test mit Rollback geprüft. Die Sicherheitsprüfung meldet lediglich erwartete INFO-Hinweise für RLS ohne öffentliche Policies.

## Offizielle Grundlagen

- [Supabase: API-Zugriff und Grants](https://supabase.com/docs/guides/api/securing-your-api)
- [Supabase: API-Keys](https://supabase.com/docs/guides/getting-started/api-keys)
- [Supabase: RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [PGlite: lokale PostgreSQL-Tests](https://pglite.dev/docs/)
