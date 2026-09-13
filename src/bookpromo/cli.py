"""Local web server and setup diagnostics."""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Book Promotion: lokale Buchverwaltung.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="Lokale Einstellungen ohne Netzwerk prüfen")
    check.add_argument("--env-file", type=Path, help="Andere ENV-Datei statt .env im Arbeitsordner")
    check.add_argument("--require", choices=("supabase", "openwebui", "all"),
                       help="Fehlende Zugangsdaten als Fehler behandeln")
    serve = commands.add_parser("serve", help="Lokale Weboberfläche starten")
    serve.add_argument("--env-file", type=Path, help="Andere ENV-Datei statt .env im Arbeitsordner")
    serve.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    dbcheck = commands.add_parser("check-db", help="Ab Schritt 3.2: Supabase lesend prüfen")
    dbcheck.add_argument("--env-file", type=Path, help="Andere lokale ENV-Datei")
    aicheck = commands.add_parser("check-openwebui", help="Open-WebUI-Zugang und Modell prüfen")
    aicheck.add_argument("--env-file", type=Path, help="Andere lokale ENV-Datei")
    aicheck.add_argument("--generate", action="store_true", help="Zusätzlich eine kurze technische Testantwort erzeugen")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ValidationError as exc:
        # Never render raw exceptions, input values or validator contexts:
        # even an invalid URL or an unknown ENV key might contain a credential.
        fields = sorted({
            str(error["loc"][0]).upper()
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
            if error["loc"] and str(error["loc"][0]) in (
                "app_host", "app_port", "app_data_dir", "app_max_upload_mb", "supabase_enabled", "supabase_url", "supabase_secret_key",
                "openwebui_url", "openwebui_api_key", "openwebui_model", "openwebui_timeout_seconds", "openwebui_max_retries",
                "analysis_chunk_chars", "analysis_min_score", "analysis_max_quotes_per_chapter", "analysis_max_calls",
                "n8n_url", "n8n_api_key",
            )
        })
        print("Konfiguration ungültig. Bitte .env mit .env.example vergleichen.", file=sys.stderr)
        if fields:
            print("Betroffene Felder: " + ", ".join(fields), file=sys.stderr)
        print("Host: lokal; Port: 1–65535; Service-URLs: HTTP(S) ohne Zugangsdaten; "
              "keine unbekannten ENV-Einträge.", file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print("ENV-Datei nicht gefunden oder nicht als UTF-8 lesbar.", file=sys.stderr)
        return 2

    if args.command == "serve":
        from .server import run_server

        run_server(settings, open_browser=not args.no_browser)
        return 0

    if args.command == "check-openwebui":
        from .openwebui import OpenWebUIClient, OpenWebUIError

        async def check_ai():
            client = OpenWebUIClient(settings)
            result = await client.check()
            print(f"Open WebUI: Zugang geprüft, {len(result['models'])} Modelle verfügbar.")
            if not result["selected_model"]: raise OpenWebUIError("missing_model")
            if not result["model_available"]: raise OpenWebUIError("model")
            if args.generate:
                await client.smoke_test(verify_model=False)
                print("Technische Textantwort erfolgreich geprüft. Keine Buchtexte übertragen.")
            else:
                print("Konfiguriertes Modell verfügbar. Keine Textgenerierung gestartet.")
        try:
            asyncio.run(check_ai())
        except OpenWebUIError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.command == "check-db":
        from .database import SCHEMA_VERSION, DatabaseError, SupabaseRepository

        try:
            asyncio.run(SupabaseRepository(settings).check_schema(full=True))
        except DatabaseError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Supabase: Schema v{SCHEMA_VERSION}, Tabellen, Ansichten und Lesezugriff geprüft.")
        print("Keine Daten geschrieben. RLS und Schreibrechte separat mit SQL prüfen.")
        return 0

    print("Lokale Konfiguration: OK")
    print("Supabase-Zugriff: " + ("aktiviert" if settings.supabase_enabled else "deaktiviert (bis Schritt 3.2)"))
    incomplete = False
    for service, label in (("supabase", "Supabase"), ("openwebui", "Open WebUI")):
        missing = settings.missing_for(service)
        if missing:
            print(f"{label}: noch nicht eingerichtet ({', '.join(missing)})")
            incomplete |= args.require in (service, "all")
        else:
            print(f"{label}: Einstellungen vollständig")
    print("Keine Netzwerkverbindungen geprüft; keine Daten geschrieben.")
    return 2 if incomplete else 0
