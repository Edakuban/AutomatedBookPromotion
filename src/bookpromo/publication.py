"""Validated local defaults and frozen destination snapshots for Reel publishing."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Platform = Literal["instagram", "facebook", "youtube", "tiktok"]
StorageProvider = Literal["supabase", "cloudflare_r2"]
QueueMode = Literal["daily", "scheduled"]
PLATFORMS: tuple[Platform, ...] = ("instagram", "facebook", "youtube", "tiktok")


class PlatformDefault(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    account_id: str = Field(default="", max_length=255)
    options: dict[str, bool | int | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enabled_has_account(self):
        if self.enabled and not self.account_id:
            raise ValueError("Für jede aktive Plattform muss ein Konto oder Kanal angegeben sein.")
        return self


class PublicationDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_provider: StorageProvider = "supabase"
    instagram: PlatformDefault = Field(default_factory=lambda: PlatformDefault(
        options={"share_to_feed": True, "is_ai_generated": True},
    ))
    facebook: PlatformDefault = Field(default_factory=lambda: PlatformDefault(
        options={"share_to_feed": True},
    ))
    youtube: PlatformDefault = Field(default_factory=lambda: PlatformDefault(
        options={"privacy_status": "private", "category_id": "22", "made_for_kids": False,
                 "notify_subscribers": False},
    ))
    tiktok: PlatformDefault = Field(default_factory=lambda: PlatformDefault(
        options={"privacy_level": "SELF_ONLY", "allow_comment": True,
                 "allow_duet": False, "allow_stitch": False},
    ))

    def selected(self) -> tuple[Platform, ...]:
        return tuple(platform for platform in PLATFORMS if getattr(self, platform).enabled)

    def platform(self, name: Platform) -> PlatformDefault:
        return getattr(self, name)

    def as_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str) -> "PublicationDefaults":
        return cls.model_validate_json(value)


@dataclass(frozen=True)
class StoredPublicationDefaults:
    revision: int
    values: PublicationDefaults


def platform_options(platform: Platform, raw: dict[str, str | bool]) -> dict[str, bool | int | str]:
    """Validate form values into the small JSON snapshot consumed by n8n."""
    if platform == "instagram":
        return {"share_to_feed": bool(raw.get("share_to_feed")), "is_ai_generated": True}
    if platform == "facebook":
        return {"share_to_feed": bool(raw.get("share_to_feed"))}
    if platform == "youtube":
        privacy = str(raw.get("privacy_status", "private"))
        if privacy not in {"private", "unlisted", "public"}:
            raise ValueError("Ungültige YouTube-Sichtbarkeit.")
        category = str(raw.get("category_id", "22")).strip()
        if not category.isdigit() or len(category) > 3:
            raise ValueError("Ungültige YouTube-Kategorie.")
        return {
            "privacy_status": privacy, "category_id": category,
            "made_for_kids": bool(raw.get("made_for_kids")),
            "notify_subscribers": bool(raw.get("notify_subscribers")),
        }
    privacy = str(raw.get("privacy_level", "SELF_ONLY"))
    if privacy not in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}:
        raise ValueError("Ungültige TikTok-Sichtbarkeit.")
    return {
        "privacy_level": privacy,
        "allow_comment": bool(raw.get("allow_comment")),
        "allow_duet": bool(raw.get("allow_duet")),
        "allow_stitch": bool(raw.get("allow_stitch")),
    }


def compact_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
