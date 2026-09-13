import json
from pathlib import Path

import pytest

from bookpromo.overlay import CANVAS_SIZE
from tools.build_n8n import INSTAGRAM_TOKEN_PLACEHOLDER, MODES, build, build_all


ROOT = Path(__file__).resolve().parent.parent


def nodes(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def targets(workflow, source, branch=0):
    return [edge["node"] for edge in workflow["connections"][source]["main"][branch]]


@pytest.mark.parametrize("mode", MODES)
def test_committed_workflows_match_generator(mode):
    committed = json.loads(
        (ROOT / "n8n" / f"book-promotion-{mode}.json").read_text(encoding="utf-8")
    )
    assert committed == build(mode)
    assert committed["active"] is False


def test_legacy_single_image_export_was_removed():
    assert not (ROOT / "n8n" / "book-promotion.json").exists()


def test_repeated_builds_are_deterministic_and_independent():
    first = build_all()
    second = build_all()
    assert first == second
    assert first["review"] is not first["auto"]


@pytest.mark.parametrize("mode", MODES)
def test_export_has_only_placeholder_credentials_and_no_external_image_host(mode):
    workflow = build(mode)
    serialized = json.dumps(workflow)
    assert "uguu" not in serialized.lower()
    assert "Bearer " not in serialized
    assert serialized.count(INSTAGRAM_TOKEN_PLACEHOLDER) == 2
    assert "/storage/v1/object/sign/" in serialized
    assert "book-promotion-media" in serialized
    for node in workflow["nodes"]:
        for credential in node.get("credentials", {}).values():
            assert credential["id"].startswith("REPLACE_")
    assert workflow["settings"]["saveDataErrorExecution"] == "none"
    assert workflow["settings"]["saveDataSuccessExecution"] == "none"


def test_review_has_hitl_and_auto_has_no_waiting_approval_nodes():
    review = nodes(build("review"))
    auto = nodes(build("auto"))
    for name in (
        "Telegram decisions",
        "Approve text",
        "Show carousel album",
        "Approve carousel",
        "Parse callback",
        "Save decision",
    ):
        assert name in review
        assert name not in auto
    assert review["Show carousel album"]["parameters"]["operation"] == "sendMediaGroup"
    assert "telegram_media" in review["Show carousel album"]["parameters"]["media"]


@pytest.mark.parametrize("mode", MODES)
def test_reservation_uses_v5_execution_mode(mode):
    workflow = build(mode)
    reserve = nodes(workflow)["Reserve quote"]
    assert reserve["parameters"]["url"].endswith("/rest/v1/rpc/bookpromo_reserve' }}")
    assert f"p_execution_mode:'{mode}'" in reserve["parameters"]["jsonBody"]
    assert reserve["parameters"]["jsonBody"].startswith("={{ {")
    assert reserve["parameters"]["jsonBody"].endswith("} }}")


@pytest.mark.parametrize("mode", MODES)
def test_http_json_body_expressions_use_n8n_expression_prefix(mode):
    for node in build(mode)["nodes"]:
        body = node.get("parameters", {}).get("jsonBody")
        if isinstance(body, str) and "$" in body:
            assert body.startswith("={{"), f"{node['name']} has malformed JSON expression"
            assert not body.startswith("={ {"), f"{node['name']} has split expression braces"


@pytest.mark.parametrize("mode", MODES)
def test_base_image_is_cropped_without_distortion_and_normalized(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    crop = by_name["Crop base to 4:5"]["parameters"]
    resize = by_name["Resize base to 1080x1350"]["parameters"]
    validate = by_name["Validate normalized base"]["parameters"]["jsCode"]
    assert crop["width"] * 5 == crop["height"] * 4
    assert crop["positionX"] * 2 + crop["width"] == 1024
    assert crop["positionY"] * 2 + crop["height"] == 1024
    assert (resize["width"], resize["height"]) == CANVAS_SIZE
    assert resize["resizeOption"] == "ignoreAspectRatio"
    assert "size.width!==1080" in validate
    assert "size.height!==1350" in validate
    assert targets(workflow, "Convert base to JPEG") == ["Crop base to 4:5"]
    assert targets(workflow, "Crop base to 4:5") == ["Resize base to 1080x1350"]


@pytest.mark.parametrize("mode", MODES)
def test_sentence_split_has_integrity_and_slide_budget_guards(mode):
    source = nodes(build(mode))["Split quote into slides"]["parameters"]["jsCode"]
    assert "new Intl.Segmenter('de',{granularity:'sentence'})" in source
    assert "[;:,—–]" in source
    assert "maxChars=30,maxLines=8" in source
    assert "render_text:rendered.join('\\n')" in source
    assert "maxChars-1).join('')+'-'" in source
    assert "slides.length>8" in source
    assert "Quote integrity check failed" in source
    assert "text:seg[i]" in source


@pytest.mark.parametrize("mode", MODES)
def test_hero_quote_and_cta_render_contract(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    quote_panel = by_name["Quote label panel"]["parameters"]
    quote_text = by_name["Add quote text"]["parameters"]
    assert quote_panel["color"] == "#11111133"
    assert quote_panel["cornerRadius"] == 28
    assert quote_text["fontColor"] == "#FFFFFF"
    assert quote_text["lineLength"] == 200
    assert quote_text["text"] == "={{ $json.render_text }}"
    assert quote_text["fontSize"] == 52
    assert quote_text["options"]["font"].endswith("/Arial.ttf")
    assert targets(workflow, "Composite title overlay") == [
        "Hero chapter panel",
        "Split quote into slides",
    ]
    assert targets(workflow, "Add hero chapter label") == ["Hero slide"]
    assert targets(workflow, "Add quote text") == ["Quote slide"]
    quote_slide = by_name["Quote slide"]["parameters"]["jsCode"]
    assert "$input.all().map" in quote_slide
    assert "$input.first()" not in quote_slide
    assert targets(workflow, "Quote manifest summary") == ["Download CTA slide"]
    assert "Add hero chapter label" not in targets(workflow, "Add quote text")
    cta = by_name["CTA slide"]["parameters"]["jsCode"]
    assert "kind:'cta'" in cta
    assert "position=d.slide_count+1" in cta


@pytest.mark.parametrize("mode", MODES)
def test_manifest_and_private_storage_upload_match_v5_contract(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    manifest = by_name["Final carousel manifest"]["parameters"]["jsCode"]
    upload = by_name["Upload carousel media"]["parameters"]
    media_ready = by_name["Media ready request"]["parameters"]["jsCode"]
    assert "items.length<3||items.length>10" in manifest
    assert "sha256(bytes)" in manifest
    assert "String(i).padStart(2,'0')+'-'+digest+'.jpg'" in manifest
    assert "/storage/v1/object/" in upload["url"]
    assert upload["contentType"] == "binaryData"
    assert upload["inputDataFieldName"] == "data"
    assert {h["name"]: h["value"] for h in upload["headerParameters"]["parameters"]} == {
        "Content-Type": "image/jpeg",
        "x-upsert": "false",
    }
    assert "p_action:'media_ready'" in media_ready
    assert targets(workflow, "Upload carousel media") == ["Verify uploaded carousel"]


@pytest.mark.parametrize("mode", MODES)
def test_instagram_children_and_parent_have_separate_fields(mode):
    by_name = nodes(build(mode))
    child_fields = {
        item["name"]: item["value"]
        for item in by_name["Create Instagram child"]["parameters"]["bodyParameters"]["parameters"]
    }
    parent_fields = {
        item["name"]: item["value"]
        for item in by_name["Create Instagram parent"]["parameters"]["bodyParameters"]["parameters"]
    }
    assert set(child_fields) == {"image_url", "is_carousel_item", "alt_text"}
    assert child_fields["is_carousel_item"] == "true"
    assert "caption" not in child_fields
    assert "is_ai_generated" not in child_fields
    assert parent_fields["media_type"] == "CAROUSEL"
    assert parent_fields["is_ai_generated"] == "true"
    assert "children" in parent_fields
    assert "caption" in parent_fields


@pytest.mark.parametrize("mode", MODES)
def test_instagram_publish_is_polled_and_container_ids_are_saved(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    assert by_name["Loop child media"]["type"] == "n8n-nodes-base.splitInBatches"
    assert by_name["Loop child media"]["parameters"]["batchSize"] == 1
    assert "bookpromo_media_container" in by_name["Save child container"]["parameters"]["url"]
    assert targets(workflow, "Child complete") == ["Loop child media"]
    assert "max_poll_attempts" in by_name["Poll child again?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert "max_poll_attempts" in by_name["Poll parent again?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert "carousel_container_ready" in by_name["Parent ready request"]["parameters"]["jsCode"]
    assert targets(workflow, "Parent finished?") == ["Publish Instagram carousel"]
    assert "p_action:'published'" in by_name["Published request"]["parameters"]["jsCode"]


@pytest.mark.parametrize("mode", MODES)
def test_instagram_token_is_refreshed_before_any_publish_side_effect(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    refresh = by_name["Refresh token for insta"]["parameters"]
    query = {
        item["name"]: item["value"]
        for item in refresh["queryParameters"]["parameters"]
    }
    assert refresh["url"] == "https://graph.instagram.com/refresh_access_token"
    assert query == {
        "grant_type": "ig_refresh_token",
        "access_token": INSTAGRAM_TOKEN_PLACEHOLDER,
    }
    assert targets(workflow, "Publication gate") == ["Instagram refresh context"]
    assert targets(workflow, "Resume publishing context") == ["Instagram refresh context"]
    assert targets(workflow, "Instagram refresh context") == ["Refresh token for insta"]
    assert targets(workflow, "Refresh token for insta") == ["Instagram access token"]
    assert targets(workflow, "Instagram access token") == ["Resume after token refresh?"]
    assert targets(workflow, "Resume after token refresh?") == ["Active publishing context"]
    assert targets(workflow, "Resume after token refresh?", 1) == ["Begin publishing request"]
    for name in (
        "Create Instagram child",
        "Instagram child status",
        "Create Instagram parent",
        "Instagram parent status",
        "Publish Instagram carousel",
        "Read Instagram permalink",
    ):
        params = by_name[name]["parameters"]
        access_tokens = [
            item["value"]
            for item in params["queryParameters"]["parameters"]
            if item["name"] == "access_token"
        ]
        assert access_tokens == [
            "={{ $('Instagram access token').first().json.access_token }}"
        ]
        assert "credentials" not in by_name[name]


@pytest.mark.parametrize("mode", MODES)
def test_signed_url_is_verified_and_cleanup_follows_published_transition(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    sign = by_name["Sign Meta URL"]["parameters"]
    verify = by_name["Validate signed media"]["parameters"]["jsCode"]
    delete = by_name["Delete temporary media"]["parameters"]
    assert "/storage/v1/object/sign/" in sign["url"]
    assert "signed_url_seconds" in sign["jsonBody"]
    assert "sha256(bytes)!==row.sha256" in verify
    assert delete["method"] == "DELETE"
    assert "prefixes" in delete["jsonBody"]
    assert targets(workflow, "Save published") == ["Published cleanup context"]
    assert targets(workflow, "Published cleanup context") == ["Read cleanup media"]
    assert "bookpromo_media_cleanup" in by_name["Save media cleanup"]["parameters"]["url"]


@pytest.mark.parametrize("mode", MODES)
def test_dry_run_gate_and_uncertain_route_preserve_media(mode):
    workflow = build(mode)
    by_name = nodes(workflow)
    config = by_name["Config"]["parameters"]["jsCode"]
    assert "publish_enabled:false" in config
    assert targets(workflow, "Publication gate") == ["Instagram refresh context"]
    assert targets(workflow, "Publication gate", 1) == ["Publication paused"]
    uncertain = by_name["Publish uncertain request"]["parameters"]["jsCode"]
    assert "p_action:'publish_uncertain'" in uncertain
    assert "Delete temporary media" not in targets(workflow, "Save publish uncertain")


def test_common_carousel_nodes_are_generated_from_same_building_blocks():
    review = nodes(build("review"))
    auto = nodes(build("auto"))
    shared = (
        "Split quote into slides",
        "Quote label panel",
        "Add quote text",
        "Final carousel manifest",
        "Upload carousel media",
        "Create Instagram child",
        "Create Instagram parent",
        "Publish Instagram carousel",
        "Delete temporary media",
    )
    for name in shared:
        assert review[name]["type"] == auto[name]["type"]
        assert review[name]["parameters"] == auto[name]["parameters"]
