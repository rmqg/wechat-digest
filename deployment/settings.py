#!/usr/bin/env python3
"""Portable project settings for public deployment tools."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("WECHAT_DIGEST_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
RUNTIME = Path(os.environ.get("WECHAT_DIGEST_RUNTIME_ROOT", ROOT / "runtime")).expanduser().resolve()
CONFIG_PATH = Path(os.environ.get("WECHAT_DIGEST_CONFIG", RUNTIME / "project.json")).expanduser()


DEFAULTS: dict[str, Any] = {
    "report_prefix": "wechat-digest", "desktop_container": "wechat-digest-desktop",
    "public_url": None, "image_origin_host": None, "daily_hour": 5, "timezone": "Asia/Shanghai",
    "worker_uid": 1000, "worker_gid": 1000,
    "images": {"runtime": "wechat-digest-runtime:1.0.0", "keyscan": "wechat-digest-runtime:1.0.0",
               "desktop": "ghcr.io/nickrunning/wechat-selkies:0.0.12-minimal"},
}


def runtime_image(name: str, root: Path | str = ROOT) -> str:
    """Read the immutable image ID pinned by explicit bootstrap."""
    config = load_project(root=root)
    reference = config["images"].get(name)
    if not isinstance(reference, str) or not reference.strip() or reference.startswith("REPLACE_WITH_"):
        raise ValueError(f"images.{name} must name a local image reference")
    root_path = Path(root).expanduser().resolve()
    pins_path = (RUNTIME if root_path == ROOT else root_path / "runtime") / "image-pins.json"
    try:
        pins = json.loads(pins_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("Image pins are missing; run `python3 deployment/bootstrap.py --pin-images` after building local images") from None
    record = pins.get("images", {}).get(name) if isinstance(pins, dict) else None
    if not isinstance(record, dict) or record.get("reference") != reference:
        raise ValueError(f"Image pin for {name} is missing or stale; after an intentional upgrade run `python3 deployment/bootstrap.py --refresh-pins`")
    image_id = record.get("id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError(f"Image pin for {name} is not an immutable Docker image ID")
    return image_id

def load_project(required: bool = False, root: Path | str = ROOT) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    configured = os.environ.get("WECHAT_DIGEST_CONFIG")
    path = Path(configured).expanduser() if configured else (CONFIG_PATH if root == ROOT else root / "runtime" / "project.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ValueError(f"project configuration is required: {path}") from None
        value = {}
    if not isinstance(value, dict):
        raise ValueError("project configuration must be a JSON object")
    config = {**DEFAULTS, **value}
    for field in ("group", "bot", "images"):
        if not isinstance(config.get(field, {}), dict):
            raise ValueError(f"project {field} configuration must be an object")
    config["group"] = config.get("group", {})
    config["bot"] = config.get("bot", {})
    config["images"] = {**DEFAULTS["images"], **config.get("images", {})}
    if required:
        for field in ("id", "name"):
            item = config["group"].get(field)
            if not isinstance(item, str) or not item.strip() or item.startswith("REPLACE_WITH_"):
                raise ValueError(f"explicit group.{field} is required for this operation")
    return config



def verify_desktop_container(config: dict[str, Any] | None = None) -> str:
    config = config or load_project()
    container = config["desktop_container"]
    expected = runtime_image("desktop")
    result = subprocess.run(["docker", "container", "inspect", "--format", "{{.Image}}", container],
                            check=True, capture_output=True, text=True, timeout=30)
    actual = result.stdout.strip()
    if actual != expected:
        raise ValueError("running desktop container image does not match configured immutable desktop image")
    return container


def bot_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_project(required=True)
    bot = config["bot"]
    username = bot.get("username")
    if not isinstance(username, str) or not username.strip() or username.startswith("REPLACE_WITH_"):
        raise ValueError("explicit bot.username is required for sending")
    names = bot.get("names", [])
    if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("bot.names must be a list of non-empty strings")
    return {"username": username, "names": names}
