#!/usr/bin/env python3
"""Caption selected-group images with Gemini, reusing verified comparison results."""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from vision import describe_image


def _load_cache(path: Path, expected_model: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("vision comparison cache must be an array")
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("provider") != "gemini":
            continue
        result = row.get("result")
        if (row.get("source_quality") in {"original", "client_full"}
                and isinstance(row.get("sha256"), str)
                and isinstance(result, dict)
                and result.get("model") == expected_model):
            cache[row["sha256"]] = result
    return cache


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _config_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _error_info(exc: Exception) -> dict[str, Any]:
    category = getattr(exc, "category", "provider_error")
    retryable = getattr(exc, "retryable", not isinstance(exc, ValueError))
    status = getattr(exc, "status_code", None)
    retry_after = getattr(exc, "retry_after", None)
    # Exception text may contain response material; persist only classified evidence.
    message = "provider request failed" if retryable else "response or input failed validation"
    return {"category": str(category), "retryable": bool(retryable), "status_code": status,
            "retry_after": retry_after if isinstance(retry_after, (int, float)) else None,
            "message": message}


def _failure_key(sha: str, model: str) -> str:
    return f"{sha}:{model}"


def caption_manifest(manifest: list[dict[str, Any]], primary_config: dict[str, Any],
                     fallback_config: dict[str, Any] | None,
                     comparison_cache: dict[str, dict[str, Any]], *,
                     on_result: Callable[[dict[str, Any] | None, dict[str, Any] | None], None] | None = None,
                     max_images: int = 0, failures: dict[str, Any] | None = None,
                     diagnostics_dir: Path | None = None,
                     ledger_lock: threading.RLock | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    output: dict[str, Any] = {}
    new_cache: dict[str, Any] = {}
    failure_ledger = failures if failures is not None else {}
    lock = ledger_lock or threading.RLock()
    cooldowns = failure_ledger.setdefault("_provider_cooldowns", {})
    if not isinstance(cooldowns, dict):
        cooldowns = {}
        failure_ledger["_provider_cooldowns"] = cooldowns
    configs = [("gemini", primary_config)]
    if fallback_config is not None:
        configs.append(("qwen", fallback_config))
    valid: dict[str, list[dict[str, Any]]] = {}
    jobs: dict[str, tuple[str, str]] = {}
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("message_uid"), str):
            raise ValueError("each image manifest entry requires message_uid")
        if item.get("available") and item.get("source_quality") in {"original", "client_full"}:
            sha, path = item.get("sha256"), item.get("image_path")
            if not isinstance(sha, str) or not isinstance(path, str):
                raise ValueError("available images require sha256 and image_path")
            valid.setdefault(sha, []).append(item)
            if sha not in comparison_cache:
                jobs.setdefault(sha, (path, item["source_quality"]))
    terminal_refusals: dict[str, dict[str, Any]] = {}
    now = time.time()
    selected = []
    cooldown_pending: set[str] = set()
    for sha, pair in jobs.items():
        with lock:
            refusal = next((row for key, row in failure_ledger.items()
                            if key.startswith(f"{sha}:") and isinstance(row, dict)
                            and row.get("category") in {"refusal", "content_filter"}), None)
        if refusal:
            terminal_refusals[sha] = refusal
            continue
        if max_images and len(selected) >= max_images:
            continue
        path, quality = pair
        available_provider = False
        for name, config in configs:
            model = str(config.get("model", ""))
            endpoint = str(config.get("base_url", config.get("url", "")))
            endpoint_fp = hashlib.sha256(endpoint.encode()).hexdigest()
            cooldown_key = f"{name}:{model}:{endpoint_fp}"
            fingerprint = _config_fingerprint(config)
            with lock:
                image_failure = failure_ledger.get(_failure_key(sha, model), {})
                cooldown = cooldowns.get(cooldown_key, {})
            if image_failure.get("category") in {"refusal", "content_filter"}:
                terminal_refusals[sha] = image_failure
                available_provider = False
                break
            auth_changed = (cooldown.get("category") == "authentication"
                            and cooldown.get("config_fingerprint") != fingerprint)
            if auth_changed or float(cooldown.get("retry_at", 0) or 0) <= now:
                if float(image_failure.get("retry_at", 0) or 0) <= now or (
                        image_failure.get("category") == "authentication"
                        and image_failure.get("config_fingerprint") != fingerprint):
                    available_provider = True
        if available_provider and sha not in terminal_refusals:
            selected.append((sha, path, quality))
        elif not available_provider and sha not in terminal_refusals:
            cooldown_pending.add(sha)
    def describe_live(sha: str, path: str, quality: str):
        reasons = []
        for name, config in configs:
            model = str(config.get("model", ""))
            key = _failure_key(sha, model)
            fingerprint = _config_fingerprint(config)
            endpoint = str(config.get("base_url", config.get("url", "")))
            endpoint_fp = hashlib.sha256(endpoint.encode()).hexdigest()
            cooldown_key = f"{name}:{model}:{endpoint_fp}"
            with lock:
                prior = dict(failure_ledger.get(key, {}))
                cooldown = dict(cooldowns.get(cooldown_key, {}))
            if prior.get("category") in {"refusal", "content_filter"}:
                reasons.append({"provider": name, "model": model, "category": prior["category"],
                                "retryable": False, "status_code": prior.get("status_code"),
                                "retry_after": None, "message": prior.get("message", "provider refusal")})
                return None, None, reasons
            auth_changed = (cooldown.get("category") == "authentication"
                            and cooldown.get("config_fingerprint") != fingerprint)
            retry_at = max(float(cooldown.get("retry_at", 0) or 0),
                           float(prior.get("retry_at", 0) or 0))
            if retry_at > time.time() and not auth_changed:
                category = cooldown.get("category", prior.get("category", "provider_error"))
                reasons.append({"provider": name, "model": model, "category": category,
                                "retryable": True, "status_code": cooldown.get("status_code", prior.get("status_code")),
                                "retry_after": retry_at - time.time(), "message": "provider is in retry backoff"})
                continue
            try:
                if diagnostics_dir is None:
                    result = describe_image(path, config, quality)
                else:
                    result = describe_image(path, config, quality, diagnostics_dir=diagnostics_dir)
                if not isinstance(result, dict) or not isinstance(result.get("model"), str) or not result["model"]:
                    raise ValueError("vision result is missing its actual model")
                actual_provider = next((provider_name for provider_name, provider_config in configs
                                        if provider_config.get("model") == result["model"]), name)
                return result, actual_provider, reasons
            except Exception as exc:
                info = _error_info(exc)
                status = info["status_code"]
                category = info["category"]
                if status in (401, 403) or category in {"authentication", "auth"}:
                    category = "authentication"
                    info["category"] = category
                refusal = category in {"refusal", "content_filter"}
                if refusal:
                    info["retryable"] = False
                with lock:
                    row = dict(failure_ledger.get(key, {}))
                    attempts = int(row.get("attempts", 0)) + 1
                    if status in (401, 403) or category == "authentication":
                        delay = 86400
                    elif category == "rate_limited" and info["retry_after"] is not None:
                        delay = max(1, float(info["retry_after"]))
                    elif info["retryable"]:
                        delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
                    else:
                        delay = 86400
                    info.update(provider=name, model=model, config_fingerprint=fingerprint,
                                endpoint_fingerprint=endpoint_fp, attempts=attempts, retry_at=time.time() + delay)
                    evidence = getattr(exc, "evidence_path", None)
                    if evidence:
                        info["evidence_path"] = str(evidence)
                    failure_ledger[key] = info
                    if status in (401, 403) or category in {"authentication", "rate_limited"}:
                        cooldowns[cooldown_key] = {"category": category, "status_code": status,
                            "retry_at": time.time() + delay, "retry_after": info["retry_after"],
                            "config_fingerprint": fingerprint, "endpoint_fingerprint": endpoint_fp}
                reasons.append({key: info[key] for key in ("provider", "model", "category", "retryable", "status_code", "retry_after", "message")})
                if diagnostics_dir:
                    diagnostic = {"sha256": sha, "provider": name, "model": model,
                                  "error": {key: info[key] for key in ("category", "retryable", "status_code", "retry_after", "message")}}
                    if evidence:
                        diagnostic["evidence_path"] = str(evidence)
                    _private_json(diagnostics_dir / f"{sha}-{name}-{int(time.time())}.json", diagnostic)
                if refusal:
                    break
        return None, None, reasons

    def apply(item: dict[str, Any], sha: str, result: dict[str, Any], provider: str, source: str):
        quality = item.get("source_quality", "unknown")
        base = {key: item.get(key) for key in ("message_uid", "available", "sha256", "dimensions", "source_quality")}
        base["source_quality"] = quality
        entry = {**base, "status": "success", "model": result["model"], "provider": provider,
                 "source": source, "result": result}
        output[item["message_uid"]] = entry
        with lock:
            for name, config in configs:
                failure_ledger.pop(_failure_key(sha, str(config.get("model", ""))), None)
        if on_result and source == "live-api":
            on_result(entry, {"sha256": sha, "model": result["model"], "provider": provider,
                              "source_quality": quality, "result": result})

    for item in manifest:
        uid, quality = item["message_uid"], item.get("source_quality", "unknown")
        base = {key: item.get(key) for key in ("message_uid", "available", "sha256", "dimensions", "source_quality")}
        base["source_quality"] = quality
        if not item.get("available") or quality not in {"original", "client_full"}:
            reason = item.get("reason", "image unavailable") if not item.get("available") else "image source is not a verified full image"
            output[uid] = {**base, "status": "unavailable", "reason": reason}
        elif item.get("sha256") in terminal_refusals:
            refusal = terminal_refusals[item["sha256"]]
            output[uid] = {**base, "status": "failed",
                           "attempts": [{"category": refusal["category"], "provider": refusal.get("provider"),
                                         "model": refusal.get("model"), "retryable": False}],
                           "reason": f"provider refusal: {refusal['category']}"}
        elif item["sha256"] in comparison_cache:
            result = comparison_cache[item["sha256"]]
            provider = "qwen" if fallback_config and result.get("model") == fallback_config.get("model") and result.get("model") != primary_config.get("model") else "gemini"
            apply(item, item["sha256"], result, provider, "verified-vision-cache")
        elif item["sha256"] not in {row[0] for row in selected}:
            reason = "provider cooldown" if item["sha256"] in cooldown_pending else "not selected for this batch"
            output[uid] = {**base, "status": "pending", "reason": reason}

    def completed(sha: str, outcome):
        result, provider, reasons = outcome
        for item in valid[sha]:
            if result is not None:
                apply(item, sha, result, provider, "live-api")
            else:
                output[item["message_uid"]] = {**{key: item.get(key) for key in ("message_uid", "available", "sha256", "dimensions", "source_quality")},
                    "status": "failed", "attempts": reasons, "reason": "; ".join(f"{x['provider']}:{x['category']}" for x in reasons)}
        if on_result:
            for item in valid[sha]:
                entry = output[item["message_uid"]]
                if entry.get("status") == "failed":
                    on_result(entry, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        iterator = iter(selected)
        pending = {}
        for _ in range(2):
            try:
                sha, path, quality = next(iterator)
            except StopIteration:
                break
            pending[pool.submit(describe_live, sha, path, quality)] = sha
        while pending:
            future = next(as_completed(tuple(pending)))
            sha = pending.pop(future)
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = (None, None, [{"provider": "unknown", "model": "", **_error_info(exc)}])
            completed(sha, outcome)
            while len(pending) < 2:
                try:
                    next_sha, path, quality = next(iterator)
                except StopIteration:
                    break
                pending[pool.submit(describe_live, next_sha, path, quality)] = next_sha
    new_cache = {sha: {"sha256": sha, "model": entry["model"], "provider": entry["provider"],
                       "source_quality": entry["source_quality"], "result": entry["result"]}
                 for sha, rows in valid.items() for item in rows
                 if (entry := output.get(item["message_uid"], {})).get("status") == "success" and entry.get("source") == "live-api"}
    return output, new_cache

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/work/image-manifest.json"))
    parser.add_argument("--primary", type=Path, default=Path("/models/gemini.json"))
    parser.add_argument("--fallback", type=Path, default=Path("/models/qwen.json"))
    parser.add_argument("--comparison", type=Path, default=Path("/work/vision-comparison.json"))
    parser.add_argument("--output", type=Path, default=Path("/work/image-understanding.json"))
    parser.add_argument("--cache-output", type=Path, default=Path("/work/vision-cache.json"))
    parser.add_argument("--max-images", type=int, default=0)
    parser.add_argument("--failures", type=Path)
    parser.add_argument("--diagnostics-dir", type=Path)
    args = parser.parse_args(argv)
    args.failures = args.failures or args.cache_output.parent / "vision-failures.json"
    args.diagnostics_dir = args.diagnostics_dir or args.cache_output.parent / "vision-diagnostics"
    if args.max_images < 0:
        parser.error("--max-images must be non-negative")
    os.umask(0o077)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise SystemExit("image manifest must be an array")
    primary = json.loads(args.primary.read_text(encoding="utf-8"))
    fallback = json.loads(args.fallback.read_text(encoding="utf-8")) if args.fallback.exists() else None
    primary_model = primary.get("model")
    if not isinstance(primary_model, str) or not primary_model:
        raise SystemExit("primary vision config requires a model")
    lock_path = args.cache_output.parent / "vision-cache.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_path.parent, 0o700)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cached = _load_cache(args.comparison, primary_model)
        saved = json.loads(args.cache_output.read_text(encoding="utf-8")) if args.cache_output.exists() else {}
        if not isinstance(saved, dict):
            raise ValueError("saved vision cache must be an object")
        accepted_models = {primary_model}
        if fallback and isinstance(fallback.get("model"), str):
            accepted_models.add(fallback["model"])
        for sha, entry in saved.items():
            if (isinstance(entry, dict) and entry.get("source_quality") in {"original", "client_full"}
                    and entry.get("model") in accepted_models
                    and isinstance(entry.get("result"), dict)
                    and entry["result"].get("model") == entry["model"]):
                cached.setdefault(sha, entry["result"])
        failures = json.loads(args.failures.read_text(encoding="utf-8")) if args.failures.exists() else {}
        if not isinstance(failures, dict):
            raise ValueError("failure ledger must be an object")
        ledger_lock = threading.RLock()

        results: dict[str, Any] = {}

        def checkpoint(entry, cache_row):
            if entry is not None:
                results[entry["message_uid"]] = entry
                if cache_row:
                    saved[cache_row["sha256"]] = cache_row
            with ledger_lock:
                failure_snapshot = copy.deepcopy(failures)
            _private_json(args.cache_output, saved)
            _private_json(args.output, results)
            _private_json(args.failures, failure_snapshot)

        results, _cache = caption_manifest(manifest, primary, fallback, cached,
            on_result=checkpoint, max_images=args.max_images, failures=failures,
            diagnostics_dir=args.diagnostics_dir, ledger_lock=ledger_lock)
        # Commit full state (including pre-cached and unavailable rows) after callbacks.
        _private_json(args.cache_output, saved)
        _private_json(args.output, results)
        _private_json(args.failures, failures)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    counts = {name: sum(item.get("status") == name for item in results.values())
              for name in ("success", "failed", "unavailable", "pending")}
    pending_shas = {item.get("sha256") for item in manifest
                    if item.get("available")
                    and (row := results.get(item.get("message_uid"), {})).get("status") == "pending"
                    and row.get("reason") == "provider cooldown"}
    now = time.time()
    retry_times = []
    configured_providers = [("gemini", primary)] + ([ ("qwen", fallback) ] if fallback else [])
    cooldown_rows = failures.get("_provider_cooldowns", {})
    for sha in pending_shas:
        for key, row in failures.items():
            if key != "_provider_cooldowns" and key.startswith(f"{sha}:") and isinstance(row, dict):
                retry_at = row.get("retry_at")
                if isinstance(retry_at, (int, float)) and retry_at > now:
                    retry_times.append(retry_at)
        for name, config in configured_providers:
            model = str(config.get("model", ""))
            endpoint = str(config.get("base_url", config.get("url", "")))
            endpoint_fp = hashlib.sha256(endpoint.encode()).hexdigest()
            row = cooldown_rows.get(f"{name}:{model}:{endpoint_fp}", {})
            retry_at = row.get("retry_at") if isinstance(row, dict) else None
            if isinstance(retry_at, (int, float)) and retry_at > now:
                retry_times.append(retry_at)
    next_attempt = min(retry_times, default=None)
    print(json.dumps({"images": len(results), **counts, "next_attempt_at": next_attempt,
                      "output": str(args.output), "cache": str(args.cache_output)}, ensure_ascii=False))
    return 0 if counts["failed"] == 0 and counts["pending"] == 0 else (2 if counts["pending"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())




