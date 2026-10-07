"""Global, revision-checked AI preferences; credentials remain in ENV."""
from contextlib import closing
from dataclasses import dataclass
import sqlite3

from .image_presets import DEFAULT_PRESET, preset_snapshot
from .text_ai import provider_missing
from .uploads import UploadError


@dataclass(frozen=True)
class AIPreferences:
    revision: int
    image_preset: str
    text_provider: str


class AISettingsStore:
    def __init__(self, uploads):
        self.uploads = uploads

    def connection(self):
        self.uploads.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.uploads.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("""create table if not exists local_ai_settings (
            singleton integer primary key check(singleton=1), revision integer not null,
            image_preset text not null, text_provider text not null)""")
        connection.commit()
        return connection

    def get(self, settings):
        row = None
        if self.uploads.db_path.is_file():
            with closing(sqlite3.connect(self.uploads.db_path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                if connection.execute("select 1 from sqlite_master where type='table' and name='local_ai_settings'").fetchone():
                    row = connection.execute("select * from local_ai_settings where singleton=1").fetchone()
        if row:
            return AIPreferences(row["revision"], row["image_preset"], row["text_provider"])
        # Initial default only, never switch an explicitly saved provider.
        provider = "comfyui_qwen" if not provider_missing(settings, "comfyui_qwen") else "openwebui"
        return AIPreferences(0, DEFAULT_PRESET, provider)

    def save(self, settings, revision, image_preset, text_provider):
        try:
            preset_snapshot(image_preset)
        except ValueError as exc:
            raise UploadError(str(exc)) from None
        if text_provider not in {"openwebui", "comfyui_qwen"} or type(revision) is not int:
            raise UploadError("Bitte einen gültigen Text-KI-Provider auswählen.")
        with closing(self.connection()) as connection, connection:
            connection.execute("begin immediate")
            row = connection.execute("select revision from local_ai_settings where singleton=1").fetchone()
            if revision != (row[0] if row else 0):
                raise UploadError("Die globalen KI-Einstellungen wurden geändert. Bitte neu laden.", 409)
            connection.execute("""insert into local_ai_settings values(1,?,?,?)
                on conflict(singleton) do update set revision=excluded.revision,
                image_preset=excluded.image_preset,text_provider=excluded.text_provider""",
                (revision + 1, image_preset, text_provider))
        return self.get(settings)
