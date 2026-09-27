import copy
import io
import json

from bookpromo.comfy import (
    ComfyClient,
    ComfyResult,
    extract_output_files,
    load_workflow_from_data,
    render_template,
    with_output_prefix,
)


class FakeTransport:
    def __init__(self, histories=None):
        self.histories = list(histories or [])
        self.posts = []

    def post_json(self, url, payload):
        self.posts.append((url, payload))
        return {"prompt_id": "prompt-1"}

    def get_json(self, url):
        return self.histories.pop(0)


def test_api_workflow_is_copied_and_templates_are_recursive():
    source = {"1": {"class_type": "Test", "inputs": {"text": "{{ prompt }}", "items": ["{{duration}}"]}}}
    loaded = load_workflow_from_data(source)
    assert loaded == source and loaded is not source
    rendered = render_template(loaded, {"prompt": "hello", "duration": 10})
    assert rendered["1"]["inputs"] == {"text": "hello", "items": ["10"]}
    assert source["1"]["inputs"]["text"] == "{{ prompt }}"


def test_ui_workflow_is_converted_to_api_prompt_with_links_and_widgets():
    workflow = {
        "nodes": [
            {"id": 1, "type": "LoadImage", "inputs": [{"name": "image", "type": "STRING", "widget": {}}],
             "widgets_values": ["input.png"]},
            {"id": 2, "type": "SaveImage", "title": "Output", "inputs": [{"name": "images", "link": 9}],
             "widgets_values": ["old-prefix"]},
            {"id": 3, "type": "Note", "widgets_values": ["not executable"]},
        ],
        "links": [[9, 1, 0, 2, 0, "IMAGE"]],
    }
    prompt = load_workflow_from_data(workflow)
    assert set(prompt) == {"1", "2"}
    assert prompt["1"]["inputs"]["image"] == "input.png"
    assert prompt["2"]["inputs"] == {"images": ["1", 0], "filename_prefix": "old-prefix"}
    assert prompt["2"]["_meta"]["title"] == "Output"


def test_ui_workflow_expands_subgraph_and_rewires_its_output():
    workflow = {
        "nodes": [
            {"id": 7, "type": "subgraph-id", "inputs": [
                {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None}
            ], "widgets_values": ["scene"]},
            {"id": 8, "type": "SaveImage", "inputs": [{"name": "images", "link": 50}],
             "widgets_values": ["output"]},
        ],
        "links": [[50, 7, 0, 8, 0, "IMAGE"]],
        "definitions": {"subgraphs": [{
            "id": "subgraph-id",
            "inputs": [{"name": "text", "type": "STRING", "linkIds": [10]}],
            "outputs": [{"name": "IMAGE", "type": "IMAGE", "linkIds": [11]}],
            "nodes": [
                {"id": 1, "type": "CLIPTextEncode", "inputs": [
                    {"name": "text", "type": "STRING", "link": 10}
                ], "widgets_values": []},
                {"id": 2, "type": "PreviewImage", "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 12}
                ], "widgets_values": []},
            ],
            "links": [[10, -1, 0, 1, 0, "STRING"], [12, 1, 0, 2, 0, "IMAGE"],
                      [11, 2, 0, -2, 0, "IMAGE"]],
        }]},
    }
    prompt = load_workflow_from_data(workflow)
    assert "7" not in prompt and prompt["7_1"]["inputs"]["text"] == "scene"
    assert prompt["8"]["inputs"]["images"] == ["7_2", 0]


def test_ui_widget_defaults_skip_ksampler_control_widget_and_add_auraflow_shift():
    workflow = {
        "nodes": [
            {
                "id": 3,
                "type": "KSampler",
                "inputs": [
                    {"name": "seed", "type": "INT", "widget": {"name": "seed"}},
                    {"name": "steps", "type": "INT", "widget": {"name": "steps"}},
                ],
                "widgets_values": [1234, "randomize", 8, 1, "res_multistep", "simple", 1],
            },
            {
                "id": 11,
                "type": "ModelSamplingAuraFlow",
                "inputs": [{"name": "model", "type": "MODEL"}],
                "widgets_values": [3],
            },
        ],
        "links": [],
    }

    prompt = load_workflow_from_data(workflow)

    assert prompt["3"]["inputs"]["seed"] == 1234
    assert prompt["3"]["inputs"]["steps"] == 8
    assert prompt["3"]["inputs"]["cfg"] == 1
    assert prompt["11"]["inputs"]["shift"] == 3


def test_workflow_execution_polls_and_extracts_outputs_and_text():
    transport = FakeTransport([
        {"prompt-1": {"status": {"completed": False}}},
        {"prompt-1": {"status": {"completed": True}, "outputs": {
            "4": {"images": [{"filename": "frame.png", "subfolder": "reels"}]},
            "5": {"text": ["done"]},
        }}},
    ])
    client = ComfyClient(transport=transport)
    result = client.run_workflow({"1": {"class_type": "X", "inputs": {"value": "{{ value }}"}}},
                                 {"value": 7}, poll_interval_sec=0)
    assert result == ComfyResult("prompt-1", True, ["reels/frame.png"], ["done"], "")
    assert transport.posts[0][1]["prompt"]["1"]["inputs"]["value"] == "7"


def test_timeout_does_not_globally_interrupt_shared_comfy_by_default(monkeypatch):
    transport = FakeTransport()
    client = ComfyClient(transport=transport)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr("bookpromo.comfy.time.monotonic", lambda: next(ticks))
    result = client.run_workflow({}, timeout_sec=.5, poll_interval_sec=0)
    assert not result.ok and result.prompt_id == "prompt-1" and result.error.startswith("COMFY_TIMEOUT")
    assert all(not url.endswith("/interrupt") for url, _ in transport.posts)


def test_output_prefix_and_extraction_are_bounded_to_file_references():
    source = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "old"}}}
    updated = with_output_prefix(source, "BookPromo/reel")
    assert updated["1"]["inputs"]["filename_prefix"] == "BookPromo/reel"
    assert source["1"]["inputs"]["filename_prefix"] == "old"
    entry = {"outputs": {"1": {"videos": [
        {"filename": "clip.mp4", "subfolder": "one\\two"},
        {"filename": "clip.mp4", "subfolder": "one/two"},
        {"filename": "../unsafe.mp4", "subfolder": "safe"},
    ]}}}
    assert extract_output_files(entry) == ["one/two/clip.mp4", "safe/unsafe.mp4"]


def test_output_download_uses_comfy_view_query(tmp_path):
    requests = []

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return Response(b"video")

    client = ComfyClient(urlopen=urlopen)
    target = client.download_output("folder/clip one.mp4", tmp_path / "clip.mp4")
    assert target.read_bytes() == b"video"
    assert "filename=clip+one.mp4" in requests[0][0]
    assert "subfolder=folder" in requests[0][0]
