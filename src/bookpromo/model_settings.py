"""Update only the model in the selected private ENV file, preserving credentials."""

import os
from pathlib import Path
import re
import tempfile
import threading

from .config import Settings
from .uploads import UploadError

_lock = threading.Lock()


def save_model(settings: Settings, model: str):
    if not 1 <= len(model) <= 300 or re.search(r"[\x00-\x1f\x7f\\\"'$]", model):
        raise UploadError("Diese Modell-ID kann nicht sicher in der ENV-Datei gespeichert werden.")
    if any(name.lower() == "openwebui_model" for name in os.environ):
        raise UploadError("OPENWEBUI_MODEL ist als Prozessvariable gesetzt. Bitte diese Variable ändern und die Anwendung neu starten.", 409)
    if settings._env_path is None:
        raise UploadError("Keine ENV-Datei zugeordnet. Bitte die Anwendung mit start.bat starten.", 409)
    with _lock:
        path = settings._env_path
        raw = path.read_bytes() if path.exists() else b""
        try: content = raw.decode("utf-8-sig")
        except UnicodeError: raise UploadError("Die ENV-Datei muss als UTF-8 gespeichert sein.") from None
        pattern = r"(?mi)^[ \t]*(?:export[ \t]+)?OPENWEBUI_MODEL[ \t]*=[^\r\n]*"
        if len(re.findall(pattern, content)) > 1:
            raise UploadError("OPENWEBUI_MODEL steht mehrfach in der ENV-Datei. Bitte die doppelten Einträge bereinigen.", 409)
        line = 'OPENWEBUI_MODEL="' + model + '"'
        newline = "\r\n" if "\r\n" in content else "\n"
        content = re.sub(pattern, lambda _: line, content) if re.search(pattern, content) else content.rstrip("\r\n") + newline + line + newline
        updated = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + content.encode("utf-8")
        temporary = None
        try:
            # .env-prefixed temporary files remain covered by the Git exclusion.
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".env.", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if (path.read_bytes() if path.exists() else b"") != raw:
                raise UploadError("Die ENV-Datei wurde zwischenzeitlich geändert. Bitte erneut speichern.", 409)
            os.replace(temporary, path)
            settings.openwebui_model = model
        finally:
            if temporary: temporary.unlink(missing_ok=True)
