import json
from pathlib import Path

from tools.build_n8n import (
    INSTAGRAM_TOKEN_PLACEHOLDER,
    build_reel_prompt_helper,
    build_reel_publisher,
)


ROOT = Path(__file__).resolve().parent.parent


def nodes(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def targets(workflow, source, branch=0):
    return [edge["node"] for edge in workflow["connections"][source]["main"][branch]]


def test_committed_reel_workflows_match_generators():
    expected = {
        "book-promotion-reel-publisher.json": build_reel_publisher(),
        "book-promotion-reel-prompt-helper.json": build_reel_prompt_helper(),
    }
    for filename, generated in expected.items():
        committed = json.loads((ROOT / "n8n" / filename).read_text(encoding="utf-8"))
        assert committed == generated
        assert committed["active"] is False
        assert committed["settings"]["saveDataSuccessExecution"] == "none"
        assert committed["settings"]["saveDataErrorExecution"] == "none"


def test_publisher_is_ai_free_and_claims_daily_or_scheduled_destinations():
    workflow = build_reel_publisher()
    by_name = nodes(workflow)
    serialized = json.dumps(workflow)
    assert "langchain" not in serialized.lower()
    assert "one.intelligence" not in serialized.lower()
    assert "openai" not in serialized.lower()
    assert {"Daily 20 Berlin", "Hourly scheduled", "Manual test"} <= set(by_name)
    assert "bookpromo_reel_claim" in by_name["Claim Reel delivery"]["parameters"]["url"]
    assert "p_platform" in by_name["Claim Reel delivery"]["parameters"]["jsonBody"]
    config_code = by_name["Config"]["parameters"]["jsCode"]
    assert "enabled_platforms=['instagram','youtube']" in config_code
    assert "publish_enabled:false" in by_name["Config"]["parameters"]["jsCode"]
    assert targets(workflow, "Publication gate") == ["Claim Reel delivery"]
    assert targets(workflow, "Publication gate", 1) == ["Dry run stopped"]
    assert targets(workflow, "Validate Reel MP4") == ["Platform router"]
    assert targets(workflow, "Platform router", 2) == ["YouTube upload metadata"]
    assert targets(workflow, "YouTube upload metadata") == ["Start YouTube upload"]
    assert by_name["Start YouTube upload"]["parameters"]["jsonBody"] == "={{ $json.youtube_body }}"
    assert "publication.description" in by_name["Create Instagram Reel"]["parameters"]["bodyParameters"]["parameters"][2]["value"]


def test_unconfigured_facebook_and_tiktok_branches_are_disabled_and_not_claimed():
    by_name = nodes(build_reel_publisher())
    disabled = {
        "Start Facebook Reel", "Facebook start context", "Transfer Facebook Reel",
        "Finish Facebook Reel", "Facebook success",
        "Query TikTok creator info", "Validate TikTok creator options",
        "Initialize TikTok post", "TikTok upload context", "Upload TikTok Reel",
        "Wait TikTok", "TikTok post status", "TikTok success",
    }
    assert all(by_name[name].get("disabled") is True for name in disabled)


def test_reel_publish_is_polled_and_ambiguous_operations_are_not_retried():
    workflow = build_reel_publisher()
    by_name = nodes(workflow)
    assert "max_poll_attempts" in by_name["Instagram poll again?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert targets(workflow, "Instagram ready?") == ["Publish Instagram Reel"]
    assert targets(workflow, "Instagram poll again?", 1) == ["Uncertain transition request"]
    assert "p_action:'publish_uncertain'" in by_name["Uncertain transition request"]["parameters"]["jsCode"]
    assert targets(workflow, "Create Instagram Reel", 1) == ["Uncertain transition request"]
    assert targets(workflow, "Publish Instagram Reel", 1) == ["Uncertain transition request"]
    assert "bookpromo_reel_transition" in by_name["Save uncertain delivery"]["parameters"]["url"]


def test_reel_cleanup_only_follows_durably_confirmed_publish():
    workflow = build_reel_publisher()
    by_name = nodes(workflow)
    assert targets(workflow, "Save published delivery") == ["Asset ready for cleanup?"]
    assert targets(workflow, "Asset ready for cleanup?") == ["Cleanup from Supabase?"]
    assert targets(workflow, "Asset ready for cleanup?", 1) == ["Delivery complete"]
    assert by_name["Delete Supabase Reel"]["parameters"]["method"] == "DELETE"
    assert by_name["Delete R2 Reel"]["parameters"]["method"] == "DELETE"
    assert "credentials" not in by_name["Delete R2 Reel"]
    assert by_name["Delete R2 Reel"]["parameters"]["url"] == "={{ $json.signed_url }}"
    assert targets(workflow, "R2 cleanup pending") == ["Prepare R2 delete"]
    assert targets(workflow, "Prepare R2 delete") == ["Sign R2 delete"]
    assert targets(workflow, "Sign R2 delete") == ["Delete R2 Reel"]
    assert "bookpromo_reel_cleanup" in by_name["Save Reel cleanup"]["parameters"]["url"]
    assert targets(workflow, "Delete Supabase Reel") == ["Cleanup request"]
    assert targets(workflow, "Delete R2 Reel") == ["Cleanup request"]


def test_reel_signed_url_is_verified_against_frozen_manifest():
    by_name = nodes(build_reel_publisher())
    for name in (
        "Nothing for platform", "Claimed delivery context", "Supabase URL context",
        "R2 URL context", "Validate Reel MP4", "Force signed R2 URL",
        "Prepublish failure request",
    ):
        assert by_name[name]["parameters"]["mode"] == "runOnceForEachItem"
        assert "$input.first()" not in by_name[name]["parameters"]["jsCode"]
    assert "/storage/v1/object/sign/" in by_name["Sign Supabase Reel URL"]["parameters"]["url"]
    config = by_name["Config"]["parameters"]["jsCode"]
    assert "R2_ENDPOINT_HIER_EINTRAGEN" in config
    assert "R2_ACCESS_KEY_ID_HIER_EINTRAGEN" in config
    assert "R2_SECRET_ACCESS_KEY_HIER_EINTRAGEN" in config


def test_full_book_teasers_have_separate_limits_and_do_not_require_quote_caption():
    by_name = nodes(build_reel_publisher())
    claimed = by_name["Claimed delivery context"]["parameters"]["jsCode"]
    assert "a.source_kind!=='book_teaser'" in claimed
    assert "p.platform==='youtube'" in claimed
    validated = by_name["Validate Reel MP4"]["parameters"]["jsCode"]
    assert "a.source_kind==='book_teaser'?314572800:52428800" in validated
    assert "a.source_kind==='book_teaser'?600000:60000" in validated
    metadata = by_name["YouTube upload metadata"]["parameters"]["jsCode"]
    assert "#Shorts" not in metadata
    signer = by_name["R2 URL context"]["parameters"]["jsCode"]
    assert "AWS4-HMAC-SHA256" in signer and "config.r2_secret_access_key" in signer
    assert "$env" not in signer
    assert "new URL(" not in signer
    assert "crypto.subtle" not in signer
    assert "const sha256=" in signer and "const hmac=" in signer
    assert "endpoint.replace(/^https:\\/\\//,'').split('/')[0]" in signer
    assert "asset.public_url" in signer and "force_signed" in signer
    delete_signer = by_name["Sign R2 delete"]["parameters"]["jsCode"]
    assert "signing_method||'GET'" in delete_signer
    assert "method==='DELETE'?'r2_signed_delete'" in delete_signer
    assert targets(build_reel_publisher(), "Public R2 URL failed?") == ["Force signed R2 URL"]
    assert targets(build_reel_publisher(), "Public R2 URL failed?", 1) == ["Prepublish failure request"]
    validator = by_name["Validate Reel MP4"]["parameters"]["jsCode"]
    assert "bytes.length!==Number(a.size_bytes)" in validator
    assert "sha256(bytes)!==a.media_sha256" in validator
    assert "bytes.toString('ascii',4,8)!=='ftyp'" in validator


def test_instagram_reuses_refresh_token_pattern_and_other_credentials_are_placeholders():
    workflow = build_reel_publisher()
    by_name = nodes(workflow)
    assert targets(workflow, "Platform router") == ["Instagram refresh context"]
    assert targets(workflow, "Instagram refresh context") == ["Refresh token for insta"]
    assert targets(workflow, "Refresh token for insta") == ["Instagram access token"]
    assert targets(workflow, "Instagram access token") == ["Create Instagram Reel"]
    assert targets(workflow, "Instagram refresh context", 1) == ["Prepublish failure request"]
    assert targets(workflow, "Refresh token for insta", 1) == ["Prepublish failure request"]
    assert targets(workflow, "Instagram access token", 1) == ["Prepublish failure request"]

    refresh_parameters = by_name["Refresh token for insta"]["parameters"]
    assert refresh_parameters["url"] == "https://graph.instagram.com/refresh_access_token"
    refresh_query = {item["name"]: item["value"] for item in refresh_parameters["queryParameters"]["parameters"]}
    assert refresh_query == {"grant_type": "ig_refresh_token", "access_token": INSTAGRAM_TOKEN_PLACEHOLDER}
    assert "credentials" not in by_name["Refresh token for insta"]

    for name in ("Create Instagram Reel", "Instagram Reel status", "Publish Instagram Reel"):
        node = by_name[name]
        assert "credentials" not in node
        query = {item["name"]: item["value"] for item in node["parameters"]["queryParameters"]["parameters"]}
        assert query["access_token"] == "={{ $('Instagram access token').first().json.access_token }}"

    for name in ("Start Facebook Reel", "Start YouTube upload", "Initialize TikTok post"):
        credentials = by_name[name].get("credentials", {})
        assert credentials and all(value["id"].startswith("REPLACE_") for value in credentials.values())
    serialized = json.dumps(workflow)
    assert serialized.count(INSTAGRAM_TOKEN_PLACEHOLDER) == 2  # Refresh input plus validation guard.


def test_prompt_helper_is_authenticated_and_preserves_caption_contract():
    workflow = build_reel_prompt_helper()
    by_name = nodes(workflow)
    webhook = by_name["Authenticated prompt webhook"]
    assert webhook["parameters"]["authentication"] == "headerAuth"
    assert webhook["credentials"]["httpHeaderAuth"]["id"].startswith("REPLACE_")
    system = by_name["Generate Reel text"]["parameters"]["messages"]["messageValues"][0]["message"]
    assert "nicht vertrauenswürdige Daten" in system
    assert "addition" in system and "image_prompt" in system
    assert "konkrete Szene des Zitats" in system
    assert "image_prompt_base ist die verbindliche globale Art Direction" in system
    assert "Rendering-Stil der image_prompt_base" in system
    validator = by_name["Validate Reel prompt result"]["parameters"]["jsCode"]
    assert "caption=[d.quote,a.addition" in validator
    assert "caption.length" not in validator  # Unicode-aware 2200-character check is used.
    assert "[...caption].length>2200" in validator
    assert targets(workflow, "Validate Reel prompt result") == ["Return Reel prompts"]


def test_v7_migration_has_private_bucket_and_guarded_rpc_contract():
    sql = (ROOT / "supabase" / "migrations" / "20260926170000_reel_publication_queue.sql").read_text(encoding="utf-8")
    assert "Schema v7 requires schema v6" in sql
    assert "'book-promotion-reels', 'book-promotion-reels', false" in sql
    assert "create function public.bookpromo_reel_claim" in sql
    assert "for update skip locked" in sql
    assert "status in ('publishing', 'publish_uncertain')" in sql
    assert "reel_publications_quote_idx" in sql and "reel_publications_book_idx" in sql
    assert "status = 'publish_uncertain'" in sql
    assert "create function public.bookpromo_reel_cleanup" in sql
    assert "reel.status <> 'published' or reel.instagram_media_id is null" in sql
    assert "update public.bookpromo_schema set version = 7 where version = 6" in sql


def test_v8_migration_separates_assets_destinations_and_safe_cleanup():
    sql = (ROOT / "supabase" / "migrations" / "20260927120000_reel_multiplatform_storage.sql").read_text(encoding="utf-8")
    assert "Schema v8 requires schema v7" in sql
    assert "create table public.reel_assets" in sql
    assert "create table public.reel_publications" in sql
    assert "platform in ('instagram','facebook','youtube','tiktok')" in sql
    assert "queue_mode in ('daily','scheduled')" in sql
    assert "create function public.bookpromo_reel_enqueue" in sql
    assert "for update skip locked" in sql
    assert "status not in ('published','cancelled')" in sql
    assert "media_status='cleanup_pending'" in sql
    assert "update public.bookpromo_schema set version=8 where version=7" in sql


def test_v9_migration_supports_chapter_reels_and_sixty_second_assets():
    sql = (ROOT / "supabase" / "migrations" / "20260930090000_chapter_reel_sources.sql").read_text(
        encoding="utf-8"
    )
    assert "Schema v9 requires schema v8" in sql
    assert "add column chapter_id uuid references public.chapters" in sql
    assert "source_kind in ('quote','chapter')" in sql
    assert "duration_ms between 4000 and 60000" in sql
    assert "create or replace function public.bookpromo_reel_enqueue" in sql
    assert "security invoker set search_path = ''" in sql
    assert "update public.bookpromo_schema set version=9 where version=8" in sql


def test_claim_patch_supports_one_worker_for_multiple_accounts():
    sql = (ROOT / "supabase" / "migrations" / "20260927130000_reel_claim_all_accounts.sql").read_text(encoding="utf-8")
    assert "account_filter text:=nullif(btrim(p_account),'')" in sql
    assert "account_filter is null or account_id=account_filter" in sql
    assert "for update skip locked" in sql
