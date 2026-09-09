"""Refresh the private n8n import without exposing existing credentials or token."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "n8n" / "book-promotion.json"
PRIVATE = ROOT / "outputs" / "n8n" / "book-promotion.private.json"
LIVE_SNAPSHOT = ROOT / "outputs" / "n8n" / "diagnostics" / "WQBWa9rGoYqGwWvf-after-publish.json"
LIVE_UPDATE = ROOT / "outputs" / "n8n" / "book-promotion.live-update.json"


def refresh() -> int:
    public = json.loads(PUBLIC.read_text(encoding="utf-8"))
    previous = json.loads(LIVE_SNAPSHOT.read_text(encoding="utf-8"))
    previous_nodes = {node["name"]: node for node in previous["nodes"]}
    preserved_credentials = 0
    for node in public["nodes"]:
        old = previous_nodes.get(node["name"])
        if old and "credentials" in old:
            node["credentials"] = old["credentials"]
            preserved_credentials += 1
        if node["name"] == "Config" and old:
            node["parameters"] = old["parameters"]
    # This is the one new credentialed node. It reuses the existing Supabase
    # reference rather than creating or exposing a second secret.
    supabase_credentials = previous_nodes["Reserve quote"]["credentials"]
    next(node for node in public["nodes"] if node["name"] == "Download title overlay")["credentials"] = supabase_credentials
    preserved_credentials += 1
    token = next(
        parameter["value"]
        for node in previous["nodes"]
        if node["name"] == "Refresh token for insta"
        for parameter in node["parameters"]["queryParameters"]["parameters"]
        if parameter["name"] == "access_token"
    )
    next(
        parameter for node in public["nodes"] if node["name"] == "Refresh token for insta"
        for parameter in node["parameters"]["queryParameters"]["parameters"]
        if parameter["name"] == "access_token"
    )["value"] = token
    PRIVATE.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update = {
        "name": previous["name"],
        "nodes": public["nodes"],
        "connections": public["connections"],
        "settings": public["settings"] | previous.get("settings", {}),
        "pinData": previous.get("pinData", {}),
    }
    LIVE_UPDATE.write_text(json.dumps(update, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Private import refreshed: {len(public['nodes'])} nodes, {preserved_credentials} credential references preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(refresh())
