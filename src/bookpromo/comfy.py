"""Small, self-contained ComfyUI client used by reel generation.

The module deliberately does not depend on VocaVid.  It accepts both API-format
prompts and ComfyUI UI workflow exports, uploads local workflow inputs, waits
for a prompt, and downloads outputs through ComfyUI's ``/view`` endpoint.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request
import uuid


class JsonTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_json(self, url: str) -> dict[str, Any]: ...


class UrlLibTransport:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("ComfyUI returned an invalid JSON object")
        return value

    def get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("ComfyUI returned an invalid JSON object")
        return value


@dataclass(frozen=True)
class ComfyResult:
    prompt_id: str
    ok: bool
    output_files: list[str] = field(default_factory=list)
    text_outputs: list[str] = field(default_factory=list)
    error: str = ""


class ComfyClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        transport: JsonTransport | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ComfyUI base URL must be an HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrlLibTransport()
        self._urlopen = urlopen

    def check(self) -> dict[str, Any]:
        """Return ComfyUI system statistics as a lightweight connection check."""
        return self.transport.get_json(f"{self.base_url}/system_stats")

    def upload_input(self, path: str | Path, *, subfolder: str = "BookPromo/reels") -> str:
        input_path = Path(path).resolve(strict=True)
        if not input_path.is_file():
            raise ValueError("ComfyUI input must be a file")
        subfolder = _safe_server_subfolder(subfolder)
        boundary = f"----BookPromo{uuid.uuid4().hex}"
        body = _multipart_form_data(
            boundary,
            fields={"type": "input", "subfolder": subfolder, "overwrite": "true"},
            files={"image": input_path},
        )
        request = urllib.request.Request(
            f"{self.base_url}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        )
        with self._urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("ComfyUI upload returned an invalid response")
        name = str(result.get("name") or input_path.name)
        returned_subfolder = _safe_server_subfolder(str(result.get("subfolder") or subfolder))
        return (Path(returned_subfolder) / Path(name).name).as_posix() if returned_subfolder else Path(name).name

    upload_image = upload_input

    def run_workflow(
        self,
        workflow_template: dict[str, Any],
        variables: dict[str, Any] | None = None,
        *,
        poll_interval_sec: float = 1.0,
        timeout_sec: float = 1800.0,
        partial_execution_targets: list[str] | None = None,
        interrupt_on_timeout: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> ComfyResult:
        if timeout_sec <= 0 or poll_interval_sec < 0:
            raise ValueError("Invalid ComfyUI polling interval or timeout")
        prompt = render_template(workflow_template, variables or {})
        payload: dict[str, Any] = {"prompt": prompt}
        if partial_execution_targets:
            payload["partial_execution_targets"] = [str(item) for item in partial_execution_targets]
        prompt_id = ""
        try:
            submit = self.transport.post_json(f"{self.base_url}/prompt", payload)
            prompt_id = str(submit.get("prompt_id") or "")
            if not prompt_id:
                return ComfyResult("", False, error="ComfyUI did not return a prompt_id")
            if progress:
                progress(prompt_id)
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() <= deadline:
                history = self.transport.get_json(f"{self.base_url}/history/{urllib.parse.quote(prompt_id, safe='')}")
                entry = history.get(prompt_id)
                if isinstance(entry, dict):
                    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                    if status.get("status_str") == "error":
                        return ComfyResult(prompt_id, False, error=_status_error(status))
                    if status.get("completed") is True:
                        files = extract_output_files(entry)
                        return ComfyResult(prompt_id, True, files, extract_text_outputs(entry))
                if poll_interval_sec:
                    time.sleep(poll_interval_sec)
        except urllib.error.HTTPError as exc:
            return ComfyResult(prompt_id, False, error=_http_error_message(exc))
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            return ComfyResult(prompt_id, False, error=f"ComfyUI request failed: {exc}")

        if interrupt_on_timeout:
            self.interrupt()
        suffix = "; sent global interrupt" if interrupt_on_timeout else ""
        return ComfyResult(prompt_id, False, error=f"COMFY_TIMEOUT: exceeded {timeout_sec:.0f}s{suffix}")

    def wait_for_prompt(
        self, prompt_id: str, *, timeout_sec: float = 1800, poll_interval_sec: float = 1,
    ) -> ComfyResult:
        """Resume polling a known job without submitting or interrupting anything."""
        if (not isinstance(prompt_id, str) or not prompt_id or len(prompt_id) > 200
                or timeout_sec <= 0 or poll_interval_sec < 0):
            raise ValueError("Invalid ComfyUI resume request")
        deadline = time.monotonic() + timeout_sec
        try:
            while time.monotonic() <= deadline:
                history = self.transport.get_json(f"{self.base_url}/history/{urllib.parse.quote(prompt_id, safe='')}")
                entry = history.get(prompt_id)
                if isinstance(entry, dict):
                    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                    if status.get("status_str") == "error":
                        return ComfyResult(prompt_id, False, error=_status_error(status))
                    if status.get("completed") is True:
                        return ComfyResult(prompt_id, True, extract_output_files(entry), extract_text_outputs(entry))
                if poll_interval_sec:
                    time.sleep(poll_interval_sec)
        except urllib.error.HTTPError as exc:
            return ComfyResult(prompt_id, False, error=_http_error_message(exc))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return ComfyResult(prompt_id, False, error=f"ComfyUI request failed: {exc}")
        return ComfyResult(prompt_id, False, error="COMFY_TIMEOUT: existing job not yet completed; no new job submitted")

    def interrupt(self) -> None:
        """Best-effort global interrupt. Call only for a dedicated ComfyUI instance."""
        try:
            self.transport.post_json(f"{self.base_url}/interrupt", {})
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
            pass

    def output_url(self, output_path: str, *, output_type: str = "output") -> str:
        normalized = str(output_path).replace("\\", "/")
        path = Path(normalized)
        query = {"filename": path.name, "type": output_type}
        if path.parent.as_posix() not in {"", "."}:
            query["subfolder"] = path.parent.as_posix()
        return f"{self.base_url}/view?{urllib.parse.urlencode(query)}"

    def download_output(self, output_path: str, target: str | Path, *, output_type: str = "output") -> Path:
        target_path = Path(target).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(self.output_url(output_path, output_type=output_type))
        temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self._urlopen(request, timeout=300) as response, temporary.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
            temporary.replace(target_path)
        finally:
            temporary.unlink(missing_ok=True)
        return target_path


def load_workflow(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ComfyUI workflow must be a JSON object")
    return load_workflow_from_data(value)


def load_workflow_from_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize either a ComfyUI API prompt or a UI workflow export."""
    if not isinstance(data.get("nodes"), list):
        return copy.deepcopy(data)
    return _ui_workflow_to_api_prompt(data)


def _ui_workflow_to_api_prompt(data: dict[str, Any]) -> dict[str, Any]:
    links = _ui_link_lookup(data.get("links", []))
    subgraphs = {
        item.get("id"): item
        for item in data.get("definitions", {}).get("subgraphs", [])
        if isinstance(item, dict) and item.get("id")
    }
    subgraph_outputs: dict[tuple[int, int], tuple[str, int]] = {}
    prompt: dict[str, Any] = {}
    for node in data.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") not in subgraphs:
            continue
        subgraph_outputs.update(
            _expand_subgraph_instance(
                prompt,
                node,
                subgraphs[node["type"]],
                subgraphs,
                external_inputs=_subgraph_external_inputs(node, subgraphs[node["type"]], links),
            )
        )
    for node in data.get("nodes", []):
        if not isinstance(node, dict) or _is_ui_only_node(node):
            continue
        if node.get("type") in subgraphs:
            continue
        node_id = str(node.get("id"))
        class_type = node.get("type")
        if not node_id or not class_type:
            continue
        inputs = _ui_node_inputs(node, links, subgraph_outputs=subgraph_outputs)
        prompt[node_id] = {"class_type": class_type, "inputs": inputs}
        title = node.get("title") or node.get("properties", {}).get("Node name for S&R")
        if title:
            prompt[node_id]["_meta"] = {"title": str(title)}
    if not prompt:
        raise ValueError("ComfyUI UI workflow contains no executable nodes")
    return prompt


def _ui_node_inputs(
    node: dict[str, Any],
    links: dict[int, tuple[int, int]],
    *,
    subgraph_outputs: dict[tuple[int, int], tuple[str, int]] | None = None,
) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    widget_values = list(node.get("widgets_values") or [])
    widget_index = 0
    for definition in node.get("inputs", []) or []:
        if not isinstance(definition, dict) or not definition.get("name"):
            continue
        name = str(definition["name"])
        link = definition.get("link")
        if link is not None and int(link) in links:
            source_id, source_slot = links[int(link)]
            source = (subgraph_outputs or {}).get((source_id, source_slot), (str(source_id), source_slot))
            inputs[name] = [source[0], source[1]]
            continue
        if definition.get("widget") is not None:
            if widget_index < len(widget_values):
                inputs[name] = widget_values[widget_index]
                widget_index += 1
            continue
        if str(definition.get("type", "")).upper() == "STRING":
            inputs[name] = "{{ prompt }}"
    _apply_widget_defaults(str(node.get("type") or ""), widget_values, inputs)
    return inputs


def _expand_subgraph_instance(
    prompt: dict[str, Any],
    instance: dict[str, Any],
    subgraph: dict[str, Any],
    subgraphs: dict[str, dict[str, Any]],
    *,
    parent_prefix: str = "",
    external_inputs: dict[int, Any] | None = None,
) -> dict[tuple[int, int], tuple[str, int]]:
    instance_id = int(instance["id"])
    prefix = f"{parent_prefix}{instance_id}_"
    external_inputs = external_inputs if external_inputs is not None else _subgraph_external_inputs(instance, subgraph)
    links = _ui_link_lookup(subgraph.get("links", []))
    nested_outputs: dict[tuple[int, int], tuple[str, int]] = {}

    for node in subgraph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        nested = subgraphs.get(str(node.get("type", "")))
        if nested is None:
            continue
        nested_inputs = _nested_subgraph_external_inputs(
            node, nested, external_inputs, links, prefix, nested_outputs
        )
        nested_outputs.update(
            _expand_subgraph_instance(
                prompt,
                node,
                nested,
                subgraphs,
                parent_prefix=prefix,
                external_inputs=nested_inputs,
            )
        )

    for node in subgraph.get("nodes", []):
        if not isinstance(node, dict) or _is_ui_only_node(node):
            continue
        if str(node.get("type", "")) in subgraphs or int(node.get("id", -1)) < 0:
            continue
        node_id = int(node["id"])
        api_id = f"{prefix}{node_id}"
        inputs: dict[str, Any] = {}
        widget_values = list(node.get("widgets_values") or [])
        widget_index = 0
        for definition in node.get("inputs", []) or []:
            if not isinstance(definition, dict) or not definition.get("name"):
                continue
            name = str(definition["name"])
            link = definition.get("link")
            if link is not None and int(link) in external_inputs:
                inputs[name] = external_inputs[int(link)]
            elif link is not None and int(link) in links:
                source_id, source_slot = links[int(link)]
                if source_id >= 0:
                    source = nested_outputs.get((source_id, source_slot), (f"{prefix}{source_id}", source_slot))
                    inputs[name] = [source[0], source[1]]
                elif definition.get("widget") is not None and widget_index < len(widget_values):
                    inputs[name] = widget_values[widget_index]
                    widget_index += 1
            elif definition.get("widget") is not None:
                if widget_index < len(widget_values):
                    inputs[name] = widget_values[widget_index]
                    widget_index += 1
                elif str(definition.get("type", "")).upper() == "STRING":
                    inputs[name] = "{{ prompt }}"
            elif str(definition.get("type", "")).upper() == "STRING":
                inputs[name] = "{{ prompt }}"
        _apply_widget_defaults(str(node.get("type") or ""), widget_values, inputs)
        prompt[api_id] = {"class_type": node.get("type"), "inputs": inputs}
        title = node.get("title") or node.get("properties", {}).get("Node name for S&R")
        if title:
            prompt[api_id]["_meta"] = {"title": str(title)}
    return _subgraph_output_map(instance_id, prefix, subgraph, nested_outputs)


def _nested_subgraph_external_inputs(
    instance: dict[str, Any],
    subgraph: dict[str, Any],
    parent_external_inputs: dict[int, Any],
    parent_links: dict[int, tuple[int, int]],
    parent_prefix: str,
    parent_nested_outputs: dict[tuple[int, int], tuple[str, int]],
) -> dict[int, Any]:
    instance_inputs = {item.get("name"): item for item in instance.get("inputs", []) if isinstance(item, dict)}
    values: dict[int, Any] = {}
    for item in subgraph.get("inputs", []) or []:
        if not isinstance(item, dict):
            continue
        definition = instance_inputs.get(item.get("name"), {})
        link = definition.get("link")
        value: Any = "{{ prompt }}" if str(definition.get("type", "")).upper() == "STRING" else None
        if link is not None and int(link) in parent_external_inputs:
            value = parent_external_inputs[int(link)]
        elif link is not None and int(link) in parent_links:
            source_id, source_slot = parent_links[int(link)]
            source = parent_nested_outputs.get((source_id, source_slot), (f"{parent_prefix}{source_id}", source_slot))
            value = [source[0], source[1]]
        for link_id in item.get("linkIds", []) or []:
            values[int(link_id)] = value
    return values


def _subgraph_external_inputs(
    instance: dict[str, Any],
    subgraph: dict[str, Any],
    parent_links: dict[int, tuple[int, int]] | None = None,
) -> dict[int, Any]:
    definitions = {item.get("name"): item for item in instance.get("inputs", []) if isinstance(item, dict)}
    widget_values = list(instance.get("widgets_values") or [])
    widget_index = 0
    values: dict[int, Any] = {}
    for item in subgraph.get("inputs", []) or []:
        if not isinstance(item, dict):
            continue
        definition = definitions.get(item.get("name"), {})
        if not definition:
            continue
        link = definition.get("link")
        value: Any = "{{ prompt }}" if str(definition.get("type", "")).upper() == "STRING" else None
        if link is not None and parent_links and int(link) in parent_links:
            source_id, source_slot = parent_links[int(link)]
            value = [str(source_id), source_slot]
        elif definition.get("widget") is not None and widget_index < len(widget_values):
            value = widget_values[widget_index]
            widget_index += 1
        for link_id in item.get("linkIds", []) or []:
            values[int(link_id)] = value
    return values


def _subgraph_output_map(
    instance_id: int,
    prefix: str,
    subgraph: dict[str, Any],
    nested_outputs: dict[tuple[int, int], tuple[str, int]],
) -> dict[tuple[int, int], tuple[str, int]]:
    output_links: dict[int, int] = {}
    for slot, item in enumerate(subgraph.get("outputs", []) or []):
        if not isinstance(item, dict):
            continue
        for link_id in item.get("linkIds", []) or []:
            output_links[int(link_id)] = slot
    result: dict[tuple[int, int], tuple[str, int]] = {}
    for link in subgraph.get("links", []) or []:
        if isinstance(link, dict):
            link_id = int(link["id"])
            source_id, source_slot = int(link["origin_id"]), int(link["origin_slot"])
        elif isinstance(link, list) and len(link) >= 3:
            link_id, source_id, source_slot = int(link[0]), int(link[1]), int(link[2])
        else:
            continue
        if link_id in output_links:
            result[(instance_id, output_links[link_id])] = nested_outputs.get(
                (source_id, source_slot), (f"{prefix}{source_id}", source_slot)
            )
    return result


def _apply_widget_defaults(class_type: str, values: list[Any], inputs: dict[str, Any]) -> None:
    mappings = {
        "CLIPLoader": {"clip_name": 0, "type": 1, "device": 2},
        "UNETLoader": {"unet_name": 0, "weight_dtype": 1},
        "VAELoader": {"vae_name": 0},
        "EmptySD3LatentImage": {"width": 0, "height": 1, "batch_size": 2},
        "EmptyLatentImage": {"width": 0, "height": 1, "batch_size": 2},
        "KSampler": {"seed": 0, "steps": 2, "cfg": 3, "sampler_name": 4, "scheduler": 5, "denoise": 6},
        "ModelSamplingAuraFlow": {"shift": 0},
        "RandomNoise": {"noise_seed": 0},
        "SaveImage": {"filename_prefix": 0},
    }
    for name, index in mappings.get(class_type, {}).items():
        if index < len(values):
            # UI exports may contain non-API widgets (for example
            # ``control_after_generate`` directly after KSampler's seed).
            # The known positional map must therefore win over the generic
            # widget scan, otherwise later values slide into the wrong input.
            inputs[name] = values[index]


def render_template(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in variables.items():
            rendered = rendered.replace("{{ " + key + " }}", str(replacement))
            rendered = rendered.replace("{{" + key + "}}", str(replacement))
        return rendered
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, variables) for key, item in value.items()}
    return value


def with_output_prefix(workflow_template: dict[str, Any], prefix: str) -> dict[str, Any]:
    workflow = copy.deepcopy(workflow_template)
    prefix = prefix.replace("\\", "/").strip("/")
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        if str(node.get("class_type", "")).lower().startswith("save") or "filename_prefix" in inputs:
            if "filename_prefix" in inputs or str(node.get("class_type", "")).lower() == "saveimage":
                inputs["filename_prefix"] = prefix
    return workflow


def extract_output_files(history_entry: dict[str, Any]) -> list[str]:
    files: list[str] = []
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return files
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        for key in ("image", "images", "video", "videos", "audio", "audios", "gif", "gifs", "files"):
            raw = output.get(key, [])
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                name = Path(str(item["filename"])).name
                subfolder = str(item.get("subfolder") or "").replace("\\", "/").strip("/")
                reference = f"{subfolder}/{name}" if subfolder else name
                if reference not in files:
                    files.append(reference)
    return files


def extract_text_outputs(history_entry: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    outputs = history_entry.get("outputs", {})
    if not isinstance(outputs, dict):
        return texts
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        for key in ("text", "texts", "string", "strings", "result", "results"):
            value = output.get(key)
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(str(item) for item in value if item is not None)
    return texts


def output_node_ids(workflow: dict[str, Any], class_types: set[str]) -> list[str]:
    wanted = {item.casefold() for item in class_types}
    return [
        str(node_id)
        for node_id, node in workflow.items()
        if isinstance(node, dict) and str(node.get("class_type", "")).casefold() in wanted
    ]


def _ui_link_lookup(links: list[Any]) -> dict[int, tuple[int, int]]:
    lookup: dict[int, tuple[int, int]] = {}
    for link in links:
        if isinstance(link, list) and len(link) >= 3:
            lookup[int(link[0])] = (int(link[1]), int(link[2]))
        elif isinstance(link, dict):
            lookup[int(link["id"])] = (int(link["origin_id"]), int(link["origin_slot"]))
    return lookup


def _is_ui_only_node(node: dict[str, Any]) -> bool:
    return str(node.get("type", "")).casefold() in {"markdownnote", "note", "reroute", "primitive"}


def _multipart_form_data(boundary: str, fields: dict[str, str], files: dict[str, Path]) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])
    for name, path in files.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def _safe_server_subfolder(value: str) -> str:
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} or "\x00" in part for part in parts):
        raise ValueError("Invalid ComfyUI subfolder")
    return "/".join(parts)


def _status_error(status: dict[str, Any]) -> str:
    messages = status.get("messages")
    if isinstance(messages, list) and messages:
        return " ".join(str(item) for item in messages)
    return "ComfyUI workflow failed"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    # ComfyUI validation bodies are useful but can contain submitted prompts.
    # Keep the local error bounded and avoid echoing book text into logs/UI.
    return f"ComfyUI HTTP error {exc.code}: {exc.reason}"
