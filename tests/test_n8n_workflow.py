import json
from pathlib import Path

from bookpromo.overlay import CANVAS_SIZE
from tools.build_n8n import build


ROOT = Path(__file__).resolve().parent.parent


def targets(workflow, source, branch=0):
    return [edge["node"] for edge in workflow["connections"][source]["main"][branch]]


def test_committed_workflow_matches_generator():
    committed = json.loads((ROOT / "n8n" / "book-promotion.json").read_text(encoding="utf-8"))
    assert committed == build()


def test_image_pipeline_outputs_exact_instagram_portrait_before_overlays():
    workflow = build()
    nodes = {node["name"]: node for node in workflow["nodes"]}
    crop = nodes["Crop to 4:5"]["parameters"]
    resize = nodes["Resize to 1080x1350"]["parameters"]

    assert crop["width"] * 5 == crop["height"] * 4
    assert crop["positionX"] * 2 + crop["width"] == 1024
    assert crop["positionY"] * 2 + crop["height"] == 1024
    assert (resize["width"], resize["height"]) == CANVAS_SIZE
    assert resize["resizeOption"] == "ignoreAspectRatio"
    assert targets(workflow, "Convert to JPEG") == ["Crop to 4:5"]
    assert targets(workflow, "Crop to 4:5") == ["Resize to 1080x1350"]
    assert targets(workflow, "Resize to 1080x1350") == ["Base image binary"]
    assert targets(workflow, "Crop to 4:5", 1) == ["Crop to 4:5 failed"]
    assert targets(workflow, "Resize to 1080x1350", 1) == ["Resize to 1080x1350 failed"]


def test_cloudflare_image_is_active_and_openai_is_disconnected_fallback():
    workflow = build()

    assert targets(workflow, "Image request") == ["Cloudflare FLUX image"]
    assert targets(workflow, "Cloudflare FLUX image") == ["Convert to File"]
    assert targets(workflow, "Convert to File") == ["Convert to JPEG"]
    assert targets(workflow, "Generate image") == ["Convert to JPEG"]
    assert targets(workflow, "Cloudflare FLUX image", 1) == ["Generate image failed"]
    assert targets(workflow, "Convert to File", 1) == ["Generate image failed"]


def test_cloudflare_caption_is_disconnected_fallback_with_shared_validator():
    workflow = build()
    nodes = {node["name"]: node for node in workflow["nodes"]}
    validator = nodes["Validate caption"]["parameters"]["jsCode"]

    assert targets(workflow, "Text request") == ["Generate caption"]
    assert targets(workflow, "Cloudflare caption") == ["Validate caption"]
    assert targets(workflow, "Cloudflare caption", 1) == ["Generate caption failed"]
    assert "input.result?.response" in validator
    assert "JSON.parse(raw)" in validator

    # Workers AI wraps JSON output as a string inside result.response.
    source = validator.replace("const inputJson=$input.first().json;\n", "").replace("inputJson", "input")
    assert "result?.response" in source


def test_cloudflare_credentials_are_placeholders_only():
    workflow = build()
    nodes = {node["name"]: node for node in workflow["nodes"]}

    for name in ("Cloudflare FLUX image", "Cloudflare caption"):
        credential = nodes[name]["credentials"]["httpHeaderAuth"]
        assert credential["id"] == "REPLACE_httpHeaderAuth"
        assert "Bearer " not in json.dumps(nodes[name])


def test_overlay_and_completed_image_dimensions_are_guarded():
    workflow = build()
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assemble = nodes["Assemble image layers"]["parameters"]["jsCode"]
    completed = nodes["Image binary"]["parameters"]["jsCode"]
    panel = nodes["Chapter label panel"]["parameters"]

    assert "incoming.overlay?'overlay':incoming.data?'data':''" in assemble
    assert "getBinaryDataBuffer(0,overlayKey)" in assemble
    assert "width!==1080||height!==1350" in assemble
    assert "size.width!==1080||size.height!==1350" in completed
    assert 0 <= panel["startPositionX"] < panel["endPositionX"] <= CANVAS_SIZE[0]
    assert 0 <= panel["startPositionY"] < panel["endPositionY"] <= CANVAS_SIZE[1]
    assert CANVAS_SIZE[0] - panel["endPositionX"] == 64
    assert CANVAS_SIZE[1] - panel["endPositionY"] == 64


def test_instagram_container_keeps_ai_generated_label():
    container = next(node for node in build()["nodes"] if node["name"] == "Instagram container")
    fields = {item["name"]: item["value"] for item in container["parameters"]["bodyParameters"]["parameters"]}
    assert fields["is_ai_generated"] == "true"
