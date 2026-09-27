import asyncio
import hashlib

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from bookpromo.config import Settings
from bookpromo.publication import PlatformDefault, PublicationDefaults
from bookpromo.r2 import R2Client
from bookpromo.reels import ReelStore
from bookpromo.uploads import LocalUploadStore
from bookpromo.web import create_app


ACCOUNT = "a" * 32


def r2_settings(tmp_path, **changes):
    values = dict(
        app_data_dir=tmp_path / "data",
        r2_account_id=ACCOUNT,
        r2_access_key_id=SecretStr("access-key"),
        r2_secret_access_key=SecretStr("secret-key"),
        r2_endpoint=f"https://{ACCOUNT}.r2.cloudflarestorage.com",
        r2_public_base_url="https://media.example.test",
    )
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_local_publication_defaults_are_optimistic_and_validated(tmp_path):
    uploads = LocalUploadStore(tmp_path / "data", 1024 * 1024)
    store = ReelStore(uploads)
    initial = store.get_publication_defaults("cloudflare_r2")
    assert initial.revision == 0 and initial.values.storage_provider == "cloudflare_r2"
    values = PublicationDefaults(
        storage_provider="cloudflare_r2",
        instagram=PlatformDefault(enabled=True, account_id="ig-user",
                                  options={"share_to_feed": True, "is_ai_generated": True}),
    )
    saved = store.save_publication_defaults(0, values)
    assert saved.revision == 1
    assert store.get_publication_defaults().values.instagram.account_id == "ig-user"


def test_r2_upload_is_sigv4_signed_and_public_url_is_encoded(tmp_path):
    video = b"\x00\x00\x00\x18ftypisomvideo"
    digest = hashlib.sha256(video).hexdigest()
    key = f"reel id/{digest}.mp4"
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=access-key/")
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(request.content).hexdigest()
        return httpx.Response(200)

    client = R2Client(r2_settings(tmp_path), transport=httpx.MockTransport(respond))
    assert asyncio.run(client.upload_reel(key, video, digest)) == key
    assert seen[0].method == "PUT"
    assert "%20" in str(seen[0].url)
    assert client.public_url(key) == f"https://media.example.test/reel%20id/{digest}.mp4"


def test_r2_check_removes_probe_even_after_head(tmp_path):
    methods = []

    def respond(request):
        methods.append(request.method)
        return httpx.Response(200)

    client = R2Client(r2_settings(tmp_path), transport=httpx.MockTransport(respond))
    asyncio.run(client.check())
    assert methods == ["PUT", "HEAD", "DELETE"]


def test_reel_settings_page_saves_platform_snapshot_defaults(tmp_path):
    settings = Settings(_env_file=None, app_data_dir=tmp_path / "data")
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post("/settings/reels", data={
            "revision": "0", "storage_provider": "supabase",
            "enabled_instagram": "on", "account_instagram": "ig-account",
            "account_facebook": "", "account_youtube": "", "account_tiktok": "",
            "instagram_share_to_feed": "on", "youtube_privacy_status": "private",
            "youtube_category_id": "22", "tiktok_privacy_level": "SELF_ONLY",
            "tiktok_allow_comment": "on",
        }, follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/settings").text
        assert "ig-account" in page and "1 aktiv" in page
