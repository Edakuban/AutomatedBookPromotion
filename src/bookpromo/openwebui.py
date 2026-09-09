"""Bounded server-side Open WebUI client. Remote errors never cross the boundary."""

import asyncio
import json
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings

T = TypeVar("T", bound=BaseModel)
MESSAGES = {
    "configuration": "Open-WebUI-URL und API-Key fehlen oder sind ungültig.",
    "credentials": "Open WebUI hat den Zugriff abgelehnt. API-Key und API-Berechtigungen prüfen.",
    "unavailable": "Open WebUI ist derzeit nicht erreichbar. Verbindung und Server prüfen.",
    "timeout": "Open WebUI hat nicht rechtzeitig geantwortet. Bitte später erneut versuchen.",
    "busy": "Open WebUI ist ausgelastet oder begrenzt Anfragen. Bitte später erneut versuchen.",
    "endpoint": "Der Open-WebUI-Endpunkt ist nicht verfügbar. Basis-URL und Serverversion prüfen.",
    "redirect": "Open WebUI leitet die Anfrage um. Bitte die endgültige HTTPS-Basis-URL eintragen.",
    "response": "Open WebUI hat keine gültige vollständige Textantwort geliefert. Modell und Streaming-Einstellungen prüfen.",
    "models": "Die Modellliste von Open WebUI hat ein unerwartetes Format.",
    "size": "Die Open-WebUI-Antwort überschreitet das unterstützte Größenlimit.",
    "model": "Das gewählte Modell ist für diesen API-Key nicht verfügbar. Bitte die Modellliste neu laden.",
    "missing_model": "Bitte zuerst ein Open-WebUI-Modell auswählen oder OPENWEBUI_MODEL in der .env eintragen.",
    "structured": "Die KI-Antwort entspricht nicht dem angeforderten JSON-Format. Es wurde kein Ergebnis übernommen.",
    "truncated": "Die KI-Antwort wurde wegen des Ausgabelimits abgeschnitten. Bitte einen kleineren Textabschnitt verwenden.",
    "request": "Open WebUI hat die Anfrage abgelehnt. Modellunterstützung und Textumfang prüfen.",
}


class OpenWebUIError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


class ModelInfo(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    name: str = Field(min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")


class OpenWebUIClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if settings.openwebui_url is None or settings.openwebui_api_key is None:
            raise OpenWebUIError("configuration")
        key = settings.openwebui_api_key.get_secret_value()
        if not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key):
            raise OpenWebUIError("configuration")
        self._url = str(settings.openwebui_url).rstrip("/") + "/"
        self._headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        self._model = settings.openwebui_model
        self._timeout = settings.openwebui_timeout_seconds
        self._retries = settings.openwebui_max_retries
        self._transport = transport

    async def _request(self, method: str, path: str, payload=None, *, limit=2 * 1024 * 1024):
        try:
            # One total deadline includes connect/read time, retries and backoff.
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(base_url=self._url, headers=self._headers,
                    timeout=httpx.Timeout(self._timeout, connect=min(10, self._timeout)),
                    follow_redirects=False, trust_env=False, transport=self._transport) as client:
                    for attempt in range(self._retries + 1):
                        try:
                            async with client.stream(method, path, json=payload) as response:
                                status = response.status_code
                                retry_after = response.headers.get("retry-after", "")
                                if 200 <= status < 300:
                                    chunks, size = [], 0
                                    async for chunk in response.aiter_bytes():
                                        size += len(chunk)
                                        if size > limit: raise OpenWebUIError("size")
                                        chunks.append(chunk)
                                    try:
                                        value = json.loads(b"".join(chunks))
                                    except (ValueError, UnicodeError):
                                        raise OpenWebUIError("response") from None
                                    if not isinstance(value, dict): raise OpenWebUIError("response")
                                    return value
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            if attempt >= self._retries: raise OpenWebUIError("unavailable") from None
                            await asyncio.sleep(.5 * 2 ** attempt)
                            continue
                        except httpx.TimeoutException:
                            # An accepted generation may still run after a read timeout.
                            raise OpenWebUIError("timeout") from None
                        except httpx.HTTPError:
                            raise OpenWebUIError("unavailable") from None
                        if status in (401, 403): raise OpenWebUIError("credentials")
                        if 300 <= status < 400: raise OpenWebUIError("redirect")
                        if status == 404: raise OpenWebUIError("endpoint")
                        retryable = status in (429, 502, 503, 504) or (method == "GET" and status == 500)
                        if retryable and attempt < self._retries:
                            delay = .5 * 2 ** attempt
                            if retry_after:
                                # Long or date-based Retry-After is left to a later user retry.
                                try: delay = max(delay, float(retry_after))
                                except ValueError: raise OpenWebUIError("busy") from None
                                if not 0 <= delay <= 5: raise OpenWebUIError("busy")
                            await asyncio.sleep(delay)
                            continue
                        if retryable: raise OpenWebUIError("busy")
                        raise OpenWebUIError("request" if status < 500 else "unavailable")
        except TimeoutError:
            raise OpenWebUIError("timeout") from None

    async def list_models(self) -> list[ModelInfo]:
        # Some installations include large embedded metadata in every model.
        # Project only ID/name below; never persist or forward the full payload.
        payload = await self._request("GET", "api/models", limit=128 * 1024 * 1024)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) > 5000: raise OpenWebUIError("models")
        models = {}
        try:
            for row in rows:
                if not isinstance(row, dict): raise OpenWebUIError("models")
                name = row.get("name") or row.get("id")
                if isinstance(name, str):
                    # Display labels can contain line breaks in custom model names.
                    name = " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in name).split())[:500]
                model = ModelInfo(id=row.get("id"), name=name or row.get("id"))
                models[model.id] = model
        except ValidationError:
            raise OpenWebUIError("models") from None
        return sorted(models.values(), key=lambda m: (m.name.casefold(), m.id))

    async def check(self):
        models = await self.list_models()
        return {"models": [m.model_dump() for m in models], "selected_model": self._model,
                "model_available": any(m.id == self._model for m in models)}

    async def complete(self, system: str, user: str, *, max_tokens: int = 2048,
                       response_schema: dict | None = None) -> str:
        if not self._model: raise OpenWebUIError("missing_model")
        if not system.strip() or not user.strip() or len(system) + len(user) > 200_000 or not 1 <= max_tokens <= 16384:
            raise OpenWebUIError("request")
        body = {"model": self._model, "stream": False, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "tools": [], "tool_ids": [], "features": {"web_search": False, "code_interpreter": False}}
        if response_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "bookpromo_result", "schema": response_schema, "strict": True}}
        payload = await self._request("POST", "api/chat/completions", body)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise OpenWebUIError("response")
        choice = choices[0]
        if choice.get("finish_reason") == "length": raise OpenWebUIError("truncated")
        message = choice.get("message")
        if (choice.get("finish_reason") != "stop" or not isinstance(message, dict)
            or message.get("tool_calls") or message.get("function_call") or message.get("refusal")):
            raise OpenWebUIError("response")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip(): raise OpenWebUIError("response")
        return content

    async def complete_json(self, system: str, user: str, result_type: type[T], *, max_tokens=2048) -> T:
        schema = result_type.model_json_schema()

        def describe_string_limits(node):
            # Constrained decoders can close strings mid-word at maxLength.
            # Keep validation local and tell the model to shorten semantically.
            if isinstance(node, dict):
                limit = node.pop("maxLength", None)
                if limit is not None:
                    node["description"] = (node.get("description", "") +
                        f" Höchstens {limit} Zeichen. Bei Bedarf kürzer formulieren; keine Wörter oder Sätze abschneiden.").strip()
                for value in node.values(): describe_string_limits(value)
            elif isinstance(node, list):
                for value in node: describe_string_limits(value)

        describe_string_limits(schema)
        content = await self.complete(system, user, max_tokens=max_tokens, response_schema=schema)
        try:
            return result_type.model_validate_json(content, strict=True)
        except ValidationError:
            raise OpenWebUIError("structured") from None

    async def smoke_test(self, *, verify_model=True):
        if not self._model: raise OpenWebUIError("missing_model")
        if verify_model:
            models = await self.list_models()
            if not any(model.id == self._model for model in models): raise OpenWebUIError("model")
        content = await self.complete("Du prüfst eine technische Verbindung. Nutze keine Werkzeuge.",
                                      "Antworte ausschließlich mit BOOKPROMO_OK", max_tokens=128)
        if content.strip() != "BOOKPROMO_OK": raise OpenWebUIError("response")
