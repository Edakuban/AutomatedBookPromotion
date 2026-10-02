"""Selectable structured-text backends for paid and local AI work."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .comfy import ComfyClient, load_workflow
from .config import Settings, TextAIProvider
from .openwebui import OpenWebUIClient


T = TypeVar("T", bound=BaseModel)
PROVIDER_LABELS: dict[TextAIProvider, str] = {
    "openwebui": "Open WebUI (extern, ggf. kostenpflichtig)",
    "comfyui_qwen": "ComfyUI · Qwen lokal",
}


class TextAIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "request"):
        self.code = code
        super().__init__(message)


def provider_model_id(settings: Settings, provider: TextAIProvider) -> str:
    if provider == "openwebui":
        return settings.openwebui_model or ""
    if provider == "comfyui_qwen":
        return settings.comfyui_qwen_model
    raise ValueError("Unbekannter Text-KI-Provider.")


def provider_endpoint_hash(settings: Settings, provider: TextAIProvider) -> str:
    endpoint = settings.openwebui_url if provider == "openwebui" else settings.comfyui_url
    value = f"{provider}:{str(endpoint).rstrip('/')}"
    return hashlib.sha256(value.encode()).hexdigest()


def provider_missing(settings: Settings, provider: TextAIProvider) -> list[str]:
    if provider == "openwebui":
        return settings.missing_for("openwebui")
    if provider == "comfyui_qwen":
        missing = []
        if settings.comfyui_url is None:
            missing.append("COMFYUI_URL")
        if not settings.comfyui_qwen_model:
            missing.append("COMFYUI_QWEN_MODEL")
        if not settings.comfyui_qwen_workflow.is_file():
            missing.append("COMFYUI_QWEN_WORKFLOW")
        return missing
    return ["TEXT_AI_PROVIDER"]


def configured_providers(settings: Settings) -> list[dict[str, str]]:
    return [
        {
            "id": provider,
            "label": PROVIDER_LABELS[provider],
            "model": provider_model_id(settings, provider),
        }
        for provider in ("openwebui", "comfyui_qwen")
        if not provider_missing(settings, provider)
    ]


def create_text_client(
    settings: Settings,
    provider: TextAIProvider,
    *,
    model_id: str | None = None,
):
    if provider == "openwebui":
        return OpenWebUIClient(settings.model_copy(update={
            "openwebui_model": model_id or settings.openwebui_model,
        }))
    if provider == "comfyui_qwen":
        return ComfyQwenClient(settings, model_id=model_id)
    raise TextAIError("Der gespeicherte Text-KI-Provider ist unbekannt.")


class ComfyQwenClient:
    """Expose a local ComfyUI Qwen workflow through the complete_json contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_id: str | None = None,
        client: ComfyClient | None = None,
    ):
        if provider_missing(settings, "comfyui_qwen"):
            raise TextAIError(
                "Der lokale Qwen-Textworkflow ist nicht vollständig eingerichtet."
            )
        self._workflow = load_workflow(settings.comfyui_qwen_workflow)
        self._model = model_id or settings.comfyui_qwen_model
        self._timeout = settings.comfyui_qwen_timeout_seconds
        self._client = client or ComfyClient(str(settings.comfyui_url))

    async def complete_json(
        self,
        system: str,
        user: str,
        result_type: type[T],
        *,
        max_tokens: int = 2048,
    ) -> T:
        if (
            not system.strip() or not user.strip()
            or len(system) + len(user) > 200_000
            or not 1 <= max_tokens <= 16_384
        ):
            raise TextAIError("Der lokale Qwen-Auftrag ist zu groß oder ungültig.")
        schema = result_type.model_json_schema()
        prompt = (
            "SYSTEMANWEISUNG:\n" + system.strip()
            + "\n\nAUFGABE UND DATEN:\n" + user.strip()
            + "\n\nVERBINDLICHES JSON-SCHEMA:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            + "\n\nGib ausschließlich ein vollständiges JSON-Objekt aus. "
              "Kein Markdown, keine Codefences, kein Kommentar vor oder nach dem JSON."
        )
        workflow = copy.deepcopy(self._workflow)
        generators = 0
        for node in workflow.values():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            inputs = node["inputs"]
            if node.get("class_type") == "TextGenerate":
                inputs["prompt"] = prompt
                inputs["max_length"] = max_tokens
                generators += 1
            elif node.get("class_type") == "CLIPLoader":
                inputs["clip_name"] = self._model
        if generators != 1:
            raise TextAIError(
                "Der lokale Qwen-Workflow muss genau einen TextGenerate-Knoten enthalten."
            )
        result = await asyncio.to_thread(
            self._client.run_workflow,
            workflow,
            poll_interval_sec=1,
            timeout_sec=self._timeout,
        )
        if not result.ok:
            detail = " ".join(result.error.split())[:500]
            raise TextAIError(
                "Der lokale Qwen-Lauf ist fehlgeschlagen."
                + (f" {detail}" if detail else "")
            )
        if not result.text_outputs:
            raise TextAIError(
                "Der lokale Qwen-Workflow hat keinen Text zurückgegeben.", code="structured",
            )
        content = result.text_outputs[-1].strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            return result_type.model_validate_json(content, strict=True)
        except ValidationError:
            raise TextAIError(
                "Qwen hat kein Ergebnis im angeforderten JSON-Schema geliefert.",
                code="structured",
            ) from None

