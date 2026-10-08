import asyncio
import json

import pytest
from pydantic import BaseModel, ConfigDict

from bookpromo.comfy import ComfyResult
from bookpromo.config import Settings
from bookpromo.text_ai import (
    ComfyQwenClient, TextAIError, provider_endpoint_hash, provider_missing,
)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    provider: str


class FakeComfy:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def run_workflow(self, workflow, **kwargs):
        self.calls.append((workflow, kwargs))
        return ComfyResult("prompt-1", True, text_outputs=[self.text])


def configured(tmp_path):
    workflow = tmp_path / "qwen.json"
    workflow.write_text(json.dumps({
        "1": {"class_type": "TextGenerate", "inputs": {
            "prompt": "old", "max_length": 1, "clip": ["2", 0],
        }},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "old.safetensors", "type": "stable_diffusion",
        }},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
    }), encoding="utf-8")
    return Settings(
        _env_file=None,
        comfyui_qwen_workflow=workflow,
        comfyui_qwen_model="qwen-local.safetensors",
    )


def test_local_qwen_client_injects_schema_prompt_model_and_token_limit(tmp_path):
    settings = configured(tmp_path)
    fake = FakeComfy('{"ok":true,"provider":"local"}')
    client = ComfyQwenClient(settings, client=fake)

    result = asyncio.run(client.complete_json(
        "System", "Nutzerdaten", Result, max_tokens=321,
    ))

    assert result == Result(ok=True, provider="local")
    workflow, kwargs = fake.calls[0]
    assert workflow["1"]["inputs"]["max_length"] == 321
    assert "VERBINDLICHES JSON-SCHEMA" in workflow["1"]["inputs"]["prompt"]
    assert workflow["2"]["inputs"]["clip_name"] == "qwen-local.safetensors"
    assert kwargs["timeout_sec"] == settings.comfyui_qwen_timeout_seconds


def test_local_qwen_client_rejects_non_schema_output(tmp_path):
    client = ComfyQwenClient(configured(tmp_path), client=FakeComfy("kein json"))
    with pytest.raises(TextAIError, match="JSON-Schema") as error:
        asyncio.run(client.complete_json("System", "Daten", Result))
    assert error.value.validation_issues
    assert "JSON" in error.value.validation_issues[0]


def test_local_qwen_schema_diagnostics_do_not_echo_model_values(tmp_path):
    client = ComfyQwenClient(
        configured(tmp_path), client=FakeComfy('{"ok":"PRIVATE_VALUE","provider":"local"}'),
    )
    with pytest.raises(TextAIError) as error:
        asyncio.run(client.complete_json("System", "Daten", Result))
    assert error.value.code == "structured"
    assert "PRIVATE_VALUE" not in " ".join(error.value.validation_issues)


def test_local_qwen_client_accepts_comfy_preview_json_fence(tmp_path):
    fake = FakeComfy('```json\n{"ok":true,"provider":"local"}\n```')
    result = asyncio.run(
        ComfyQwenClient(configured(tmp_path), client=fake).complete_json(
            "System", "Daten", Result,
        )
    )
    assert result.provider == "local"


def test_provider_configuration_and_fingerprints_are_separate(tmp_path):
    settings = configured(tmp_path)
    assert not provider_missing(settings, "comfyui_qwen")
    assert provider_endpoint_hash(settings, "comfyui_qwen") != provider_endpoint_hash(
        settings, "openwebui"
    )
    settings.comfyui_qwen_workflow = tmp_path / "missing.json"
    assert provider_missing(settings, "comfyui_qwen") == ["COMFYUI_QWEN_WORKFLOW"]
