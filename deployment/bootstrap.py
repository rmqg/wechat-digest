#!/usr/bin/env python3
"""Initialize private runtime directories, pin local images, or run read-only preflight."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import settings


def _docker_image(reference: str) -> str:
    try:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                                check=True, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Image {reference!r} is unavailable locally. Build first with `docker compose --profile tools build runtime`, start desktop with `docker compose up -d desktop`, then run `python3 deployment/bootstrap.py --pin-images`; automatic pulls are disabled.") from exc
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError(f"Docker returned a non-immutable ID for image {reference!r}")
    return image_id


def _container_image(container: str) -> str:
    try:
        result = subprocess.run(["docker", "container", "inspect", "--format", "{{.Image}}", container],
                                check=True, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Desktop container {container!r} is not running; start it with `docker compose up -d desktop` before pinning/checking.") from exc
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError("Docker returned an invalid desktop container image ID")
    return image_id


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".image-pins-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        project = settings.load_project()
        _set_owner(Path(temporary), project["worker_uid"], project["worker_gid"])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _set_owner(path: Path, uid: int, gid: int) -> None:
    if os.geteuid() == 0:
        os.chown(path, uid, gid)
    else:
        info = path.stat()
        if info.st_uid != uid or info.st_gid != gid:
            raise RuntimeError(f"Cannot give worker {uid}:{gid} access to {path}; run bootstrap as root or set project worker_uid/worker_gid to the current owner")


def init_runtime() -> None:
    runtime = settings.RUNTIME
    worker_uid = settings.DEFAULTS["worker_uid"]
    worker_gid = settings.DEFAULTS["worker_gid"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    _set_owner(runtime, worker_uid, worker_gid)
    for name in ("gui", "keyscan", "digest", "reports", "ui-calibration"):
        directory = runtime / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        _set_owner(directory, worker_uid, worker_gid)
    fonts = runtime / "report-fonts"
    fonts.mkdir(parents=True, exist_ok=True, mode=0o755)
    project = runtime / "project.json"
    if not project.exists():
        example = settings.ROOT / "examples" / "project.example.json"
        if not example.is_file():
            raise RuntimeError(f"Configuration template is missing: {example}")
        project.write_bytes(example.read_bytes())
        os.chmod(project, 0o600)
        _set_owner(project, worker_uid, worker_gid)
        created = True
    else:
        created = False
        if os.geteuid() == 0:
            os.chown(project, worker_uid, worker_gid)
        project.chmod(0o600)
        _set_owner(project, worker_uid, worker_gid)
    print(json.dumps({"status": "initialized", "config_created": created, "runtime": str(runtime), "config": str(project),
                      "next": "configure images, start desktop, then run bootstrap --pin-images; capture/list the local databases before setting the exact target"}, ensure_ascii=False))


def _configured_images(config: dict) -> dict[str, str]:
    images = config.get("images", {})
    required = ("runtime", "keyscan", "desktop")
    result = {}
    for name in required:
        reference = images.get(name)
        if not isinstance(reference, str) or not reference.strip() or reference.startswith("REPLACE_WITH_"):
            raise RuntimeError(f"Set images.{name} in runtime/project.json before image pinning")
        result[name] = reference
    return result


def pin_images(*, refresh: bool) -> None:
    config = settings.load_project()
    runtime = settings.RUNTIME
    pin_path = runtime / "image-pins.json"
    if pin_path.exists() and not refresh:
        raise RuntimeError("image-pins.json already exists; use --refresh-pins only for an intentional image upgrade")
    if not pin_path.exists() and refresh:
        raise RuntimeError("No existing image pins to refresh; use --pin-images for initial pinning")
    references = _configured_images(config)
    pins = {name: {"reference": reference, "id": _docker_image(reference)}
            for name, reference in references.items()}
    if pins["runtime"]["id"] != pins["keyscan"]["id"]:
        raise RuntimeError("images.runtime and images.keyscan must resolve to the same locally built runtime image")
    desktop_id = _container_image(config["desktop_container"])
    if desktop_id != pins["desktop"]["id"]:
        raise RuntimeError("running desktop container image differs from images.desktop; restart the configured desktop before pinning")
    _private_json(pin_path, {"schema_version": 1, "pinned_at": int(time.time()), "images": pins})
    print(json.dumps({"status": "pinned", "pins_file": str(pin_path),
                      "images": {name: record["id"] for name, record in pins.items()}}, ensure_ascii=False))


def check() -> None:
    config = settings.load_project()
    pin_path = settings.RUNTIME / "image-pins.json"
    try:
        pins = json.loads(pin_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError("Image pins are missing; build local runtime and start desktop, then run `python3 deployment/bootstrap.py --pin-images`.") from None
    if pins.get("schema_version") != 1 or not isinstance(pins.get("images"), dict):
        raise RuntimeError("image-pins.json has an unsupported schema; inspect it before explicit --refresh-pins")
    references = _configured_images(config)
    for name, reference in references.items():
        record = pins["images"].get(name)
        if not isinstance(record, dict) or record.get("reference") != reference:
            raise RuntimeError(f"Image pin for {name} is stale; run `python3 deployment/bootstrap.py --refresh-pins` only after intentional upgrade")
        pinned_id = record.get("id")
        if not isinstance(pinned_id, str) or not pinned_id.startswith("sha256:") or len(pinned_id) != 71:
            raise RuntimeError(f"Image pin for {name} is invalid")
        current = _docker_image(reference)
        if current != pinned_id:
            raise RuntimeError(f"Configured image tag {name} now resolves to a different ID; after intentional upgrade run `python3 deployment/bootstrap.py --refresh-pins`")
    if pins["images"]["runtime"]["id"] != pins["images"]["keyscan"]["id"]:
        raise RuntimeError("runtime/keyscan image pins do not match")
    if _container_image(config["desktop_container"]) != pins["images"]["desktop"]["id"]:
        raise RuntimeError("running desktop container does not match its pinned image; stop and start the configured desktop, then explicitly refresh pins if upgrading")
    for path in (settings.RUNTIME / "gui", settings.RUNTIME / "keyscan"):
        if not path.is_dir():
            raise RuntimeError(f"Required runtime directory is missing: {path}; initialize with `python3 deployment/bootstrap.py --init-runtime`")
    selection_ready = (settings.RUNTIME / "selection.json").is_file()
    print(json.dumps({"status": "ready", "runtime": str(settings.RUNTIME), "pins_file": str(pin_path),
                      "desktop_container": config["desktop_container"], "selection_ready": selection_ready}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init-runtime", action="store_true", help="create private runtime directories and copy config hints")
    mode.add_argument("--pin-images", action="store_true", help="resolve locally built images and create immutable ID pins")
    mode.add_argument("--refresh-pins", action="store_true", help="explicitly refresh immutable IDs after intentional upgrade")
    parser.add_argument("--check", action="store_true", help="read-only preflight (default)")
    args = parser.parse_args()
    try:
        if args.init_runtime:
            init_runtime()
        elif args.pin_images:
            pin_images(refresh=False)
        elif args.refresh_pins:
            pin_images(refresh=True)
        else:
            check()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"bootstrap: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
