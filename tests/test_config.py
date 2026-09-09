from pathlib import Path

import pytest

from bookpromo.cli import main
from bookpromo.config import Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.chdir(tmp_path)


def test_first_run_needs_no_credentials_or_writes(tmp_path, capsys):
    assert main(["check-config"]) == 0
    output = capsys.readouterr().out
    assert "Lokale Konfiguration: OK" in output
    assert "SUPABASE_SECRET_KEY" in output
    assert "OPENWEBUI_MODEL" in output
    assert list(tmp_path.iterdir()) == []


def test_empty_example_is_valid(tmp_path):
    source = Path(__file__).resolve().parents[1] / ".env.example"
    (tmp_path / ".env").write_bytes(source.read_bytes())
    settings = load_settings()
    assert settings.supabase_url is None
    assert settings.openwebui_api_key is None
    assert settings.n8n_api_key is None
    assert settings.app_data_dir == tmp_path / "data"


def test_windows_bom_and_environment_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("APP_PORT=8010\nOPENWEBUI_MODEL=Dateimodell\n", encoding="utf-8-sig")
    monkeypatch.setenv("APP_PORT", "8020")
    settings = load_settings()
    assert settings.app_port == 8020
    assert settings.openwebui_model == "Dateimodell"


def test_explicit_env_anchors_relative_data_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env = config_dir / ".env.local"
    env.write_text("APP_DATA_DIR=private-books\n", encoding="utf-8")
    assert load_settings(env).app_data_dir == config_dir / "private-books"


def test_explicit_missing_file_is_an_error(capsys):
    assert main(["check-config", "--env-file", "missing.env"]) == 2
    assert "ENV-Datei" in capsys.readouterr().err


@pytest.mark.parametrize("require", ["supabase", "openwebui", "all"])
def test_required_integration_is_incomplete(require):
    assert main(["check-config", "--require", require]) == 2


def test_complete_config_does_not_expose_values(tmp_path, capsys):
    secret = "TEST_SECRET_DO_NOT_DISPLAY"
    (tmp_path / ".env").write_text(
        f"SUPABASE_URL=https://example.supabase.co\nSUPABASE_SECRET_KEY={secret}\n"
        f"OPENWEBUI_URL=http://localhost:3000\nOPENWEBUI_API_KEY={secret}\n"
        "OPENWEBUI_MODEL=private-model-name\n"
        f"N8N_URL=https://n8n.example.org\nN8N_API_KEY={secret}\n", encoding="utf-8",
    )
    assert main(["check-config", "--require", "all"]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + repr(load_settings())
    assert "private-model-name" not in captured.out + captured.err
    assert "example.supabase.co" not in captured.out + captured.err
    assert load_settings().n8n_api_key.get_secret_value() == secret


@pytest.mark.parametrize("line", [
    "APP_PORT=TEST_SECRET_DO_NOT_DISPLAY",
    "APP_PORT=70000",
    "APP_HOST=0.0.0.0",
    "APP_DATA_DIR=",
    "OPENWEBUI_URL=TEST_SECRET_DO_NOT_DISPLAY",
    "OPENWEBUI_URL=https://user:TEST_SECRET_DO_NOT_DISPLAY@example.org",
    "OPENWEBUI_URL=https://example.org?token=TEST_SECRET_DO_NOT_DISPLAY",
    "N8N_URL=https://user:TEST_SECRET_DO_NOT_DISPLAY@example.org",
    "N8N_URL=https://example.org?token=TEST_SECRET_DO_NOT_DISPLAY",
    "TYPO_KEY=TEST_SECRET_DO_NOT_DISPLAY",
])
def test_invalid_values_are_rejected_without_secret_leak(tmp_path, capsys, line):
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    assert main(["check-config"]) == 2
    captured = capsys.readouterr()
    assert "Konfiguration ungültig" in captured.err
    assert "TEST_SECRET_DO_NOT_DISPLAY" not in captured.out + captured.err


def test_parent_env_is_not_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("APP_PORT=9000\n", encoding="utf-8")
    child = tmp_path / "other-project"
    child.mkdir()
    monkeypatch.chdir(child)
    assert load_settings().app_port == 8000


def test_serve_uses_selected_configuration_without_browser(tmp_path, monkeypatch):
    from bookpromo import server

    selected = tmp_path / ".env.local"
    selected.write_text("APP_PORT=8091\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(server, "run_server", lambda settings, **kwargs: calls.append((settings, kwargs)))
    assert main(["serve", "--env-file", str(selected), "--no-browser"]) == 0
    assert calls[0][0].app_port == 8091
    assert calls[0][1] == {"open_browser": False}


def test_invalid_configuration_does_not_start_server(tmp_path, monkeypatch, capsys):
    from bookpromo import server

    (tmp_path / ".env").write_text("APP_PORT=PRIVATE_VALUE\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(server, "run_server", lambda *args, **kwargs: calls.append(True))
    assert main(["serve"]) == 2
    assert calls == []
    assert "PRIVATE_VALUE" not in capsys.readouterr().err
