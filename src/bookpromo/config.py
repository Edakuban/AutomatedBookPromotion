"""Server-side settings. Importing this module never reads files or connects."""

from pathlib import Path
import re
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Service = Literal["supabase", "openwebui", "comfyui", "r2"]
TextAIProvider = Literal["openwebui", "comfyui_qwen"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8-sig",
        case_sensitive=False,
        extra="forbid",
        hide_input_in_errors=True,
    )

    # Local-only defaults. Server hosting will be a separate implementation step.
    app_host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_data_dir: Path = Path("data")
    app_max_upload_mb: int = Field(default=25, ge=1, le=100)

    supabase_enabled: bool = False
    supabase_url: HttpUrl | None = Field(default=None, repr=False)
    supabase_secret_key: SecretStr | None = None
    openwebui_url: HttpUrl | None = Field(default=None, repr=False)
    openwebui_api_key: SecretStr | None = None
    openwebui_model: str | None = Field(default=None, repr=False, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    openwebui_timeout_seconds: float = Field(default=120, ge=5, le=600, allow_inf_nan=False)
    openwebui_max_retries: int = Field(default=2, ge=0, le=3)
    # Local ComfyUI rendering for quote reels. Workflow paths are resolved
    # relative to the selected ENV file, just like APP_DATA_DIR.
    comfyui_url: HttpUrl = "http://127.0.0.1:8188"
    comfyui_qwen_workflow: Path = Path("workflows/qwen-text.json")
    comfyui_qwen_model: str = Field(
        default="qwen3.5_4b_bf16.safetensors",
        min_length=1,
        max_length=300,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    comfyui_qwen_timeout_seconds: float = Field(
        default=900, ge=30, le=3600, allow_inf_nan=False,
    )
    reel_image_workflow: Path = Path("workflows/reel-image.json")
    reel_reference_workflow: Path = Path("workflows/reel-reference.json")
    reel_video_workflow: Path = Path("workflows/reel-video.json")
    reel_default_duration_seconds: float = Field(default=10, ge=4, le=30, allow_inf_nan=False)
    reel_max_audio_mb: int = Field(default=250, ge=1, le=1000)
    reel_max_video_mb: int = Field(default=45, ge=1, le=49)
    book_teaser_max_video_mb: int = Field(default=256, ge=1, le=300)
    reel_storage_provider: Literal["supabase", "cloudflare_r2"] = "supabase"
    r2_account_id: str | None = Field(default=None, repr=False, pattern=r"^[0-9a-f]{32}$")
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str = Field(default="book-promotion-reels", min_length=3, max_length=63)
    r2_endpoint: HttpUrl | None = Field(default=None, repr=False)
    r2_public_base_url: HttpUrl | None = Field(default=None, repr=False)
    r2_signed_url_ttl_seconds: int = Field(default=86400, ge=900, le=604800)
    # Optional diagnostics access to the user's n8n instance.
    n8n_url: HttpUrl | None = Field(default=None, repr=False)
    n8n_api_key: SecretStr | None = Field(default=None, repr=False)
    analysis_chunk_chars: int = Field(default=12000, ge=2000, le=24000)
    analysis_min_score: int = Field(default=4, ge=1, le=5)
    analysis_max_quotes_per_chapter: int = Field(default=12, ge=1, le=50)
    analysis_max_calls: int = Field(default=1000, ge=1, le=5000)
    _env_path: Path | None = PrivateAttr(default=None)

    @field_validator(
        "supabase_url", "supabase_secret_key", "openwebui_url", "comfyui_url",
        "openwebui_api_key", "openwebui_model", "n8n_url", "n8n_api_key",
        "r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_endpoint",
        "r2_public_base_url", mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator(
        "supabase_url", "openwebui_url", "comfyui_url", "n8n_url", "r2_endpoint",
        "r2_public_base_url",
    )
    @classmethod
    def plain_service_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (value.username or value.password or value.query or value.fragment):
            raise ValueError("Service-URL ohne Zugangsdaten, Query oder Fragment angeben.")
        return value

    @field_validator("r2_endpoint", "r2_public_base_url")
    @classmethod
    def secure_r2_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and value.scheme != "https":
            raise ValueError("R2-URLs müssen HTTPS verwenden.")
        return value

    @field_validator("r2_bucket")
    @classmethod
    def valid_r2_bucket(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?", value):
            raise ValueError("Ungültiger R2-Bucketname.")
        return value

    @field_validator("app_data_dir", mode="before")
    @classmethod
    def nonempty_data_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Datenverzeichnis darf nicht leer sein.")
        return value

    def missing_for(self, service: Service) -> list[str]:
        required = {
            "supabase": ("supabase_url", "supabase_secret_key"),
            "openwebui": ("openwebui_url", "openwebui_api_key", "openwebui_model"),
            "comfyui": ("comfyui_url", "reel_image_workflow", "reel_reference_workflow", "reel_video_workflow"),
            "r2": ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_endpoint"),
        }
        return [name.upper() for name in required[service] if getattr(self, name) is None]


def load_settings(env_file: Path | None = None) -> Settings:
    """Read one explicit .env, with process variables taking precedence.

    With no argument, use .env in the working directory; it may be absent.
    An explicitly requested file must exist. No parent directories are searched.
    Relative data paths are anchored to the selected env file's directory.
    """
    selected = (env_file if env_file is not None else Path.cwd() / ".env").resolve()
    if env_file is not None and not selected.is_file():
        raise FileNotFoundError("Die angegebene ENV-Datei existiert nicht.")
    settings = Settings(_env_file=selected)
    settings._env_path = selected
    if not settings.app_data_dir.is_absolute():
        settings.app_data_dir = (selected.parent / settings.app_data_dir).resolve()
    for field in (
        "comfyui_qwen_workflow", "reel_image_workflow", "reel_reference_workflow",
        "reel_video_workflow",
    ):
        path = getattr(settings, field)
        if not path.is_absolute():
            setattr(settings, field, (selected.parent / path).resolve())
    return settings
