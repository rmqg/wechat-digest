#!/usr/bin/env python3
"""Run isolated local Chinese OCR over prepared full-quality images."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

ENGINE = "rapidocr-onnxruntime"
ENGINE_VERSION = "1.4.4"
CACHE_SCHEMA = 1
OCR_MAX_SIDE_LEN = 2000
OCR_DET_LIMIT_SIDE = 736
_TILED_CACHE_SUFFIX = "-tiled-v1"


def _needs_tiled_ocr(width: int, height: int) -> bool:
    long_side = max(width, height)
    if long_side <= OCR_MAX_SIDE_LEN:
        return False
    ratio = OCR_MAX_SIDE_LEN / long_side
    scaled_short_side = round(int(min(width, height) * ratio) / 32) * 32
    return scaled_short_side < OCR_DET_LIMIT_SIDE


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _create_ocr():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)


def _valid_cached(path: Path, image_sha: str) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA
                or value.get("image_sha256") != image_sha
                or value.get("engine") != ENGINE or value.get("engine_version") != ENGINE_VERSION
                or not isinstance(value.get("text"), str) or not isinstance(value.get("lines"), list)):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _cache_path(cache_dir: Path, image_sha: str, data: bytes) -> Path:
    # Only chunked long-aspect inputs have different OCR semantics; preserve caches
    # for images that still follow RapidOCR's existing max-side preprocessing.
    from PIL import Image
    import io

    with Image.open(io.BytesIO(data)) as source:
        suffix = _TILED_CACHE_SUFFIX if _needs_tiled_ocr(*source.size) else ""
    return cache_dir / f"{image_sha}-{ENGINE}-{ENGINE_VERSION}{suffix}.json"



def _image_bytes(item: dict[str, Any]) -> tuple[bytes, Path]:
    if item.get("available") is not True:
        raise ValueError("image is unavailable")
    if item.get("source_quality") not in {"original", "client_full"}:
        raise ValueError("image is not original/client_full quality")
    raw_path = item.get("image_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("image_path is missing")
    path = Path(raw_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("image_path is not a regular file")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != item.get("sha256"):
        raise ValueError("image SHA-256 does not match manifest")
    return data, path


def _recognize(data: bytes, ocr: Any) -> tuple[str, list[dict[str, Any]], float | None]:
    # Decode the source once, then bound OCR-only working images. The vision and
    # persisted image paths continue to receive the untouched source bytes.
    from PIL import Image
    import numpy as np
    import io

    result = []
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        width, height = source.size
        if _needs_tiled_ocr(width, height):
            long_side = max(width, height)
            short_side = min(width, height)
            tile_side = min(OCR_MAX_SIDE_LEN,
                            max(1, int((OCR_MAX_SIDE_LEN - 16) * short_side / OCR_DET_LIMIT_SIDE)))
            overlap = min(256, tile_side // 8)
            stride = max(1, tile_side - overlap)
            starts = [0]
            while starts[-1] + tile_side < long_side:
                next_start = starts[-1] + stride
                if next_start + tile_side >= long_side:
                    next_start = long_side - tile_side
                if next_start <= starts[-1]:
                    break
                starts.append(next_start)

            long_axis = 1 if height >= width else 0
            for index, start in enumerate(starts):
                end = min(start + tile_side, long_side)
                tile_source = (source.crop((0, start, width, end)) if long_axis == 1
                               else source.crop((start, 0, end, height)))
                tile_rgb = tile_source.convert("RGB")
                tile_source.close()
                image = np.asarray(tile_rgb)[:, :, ::-1].copy()
                tile_rgb.close()
                tile_result, _ = ocr(image)
                core_start = (0 if index == 0 else
                              (starts[index - 1] + tile_side + start) / 2)
                core_end = (long_side if index == len(starts) - 1 else
                            (start + tile_side + starts[index + 1]) / 2)
                for entry in tile_result or []:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                        continue
                    try:
                        coordinates = [float(point[long_axis]) for point in entry[0]]
                        center = sum(coordinates) / len(coordinates) + start
                    except (TypeError, ValueError, IndexError, ZeroDivisionError):
                        result.append(entry)
                        continue
                    if not coordinates or core_start <= center < core_end:
                        result.append(entry)
        else:
            rgb = source.convert("RGB")
            source.close()
            if max(width, height) > OCR_MAX_SIDE_LEN:
                ratio = OCR_MAX_SIDE_LEN / max(width, height)
                resize_height = max(32, round(int(height * ratio) / 32) * 32)
                resize_width = max(32, round(int(width * ratio) / 32) * 32)
                rgb = rgb.resize((resize_width, resize_height), Image.Resampling.BILINEAR)
            image = np.asarray(rgb)[:, :, ::-1].copy()
            rgb.close()
            result, _ = ocr(image)

    lines: list[dict[str, Any]] = []
    for entry in result or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        text, confidence = entry[1], entry[2]
        if not isinstance(text, str):
            continue
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            continue
        if not 0.0 <= confidence <= 1.0:
            continue
        lines.append({"text": text, "confidence": confidence})
    text = "\n".join(line["text"] for line in lines)
    confidence = sum(line["confidence"] for line in lines) / len(lines) if lines else None
    return text, lines, confidence


def process_manifest(manifest: list[Any], output_path: Path, cache_dir: Path, max_images: int = 0) -> dict[str, Any]:
    if max_images < 0:
        raise ValueError("max_images must be zero or greater")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache_dir, 0o700)
    output: dict[str, Any] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    eligible: list[tuple[str, dict[str, Any]]] = []
    for index, item in enumerate(manifest):
        uid = item.get("message_uid") if isinstance(item, dict) else None
        if not isinstance(uid, str) or not uid:
            uid = f"invalid_manifest_item_{index}"
        if uid in seen:
            duplicates.add(uid)
            output[uid] = {"status": "failed", "source_kind": "ocr", "reason": "duplicate message_uid in manifest"}
            continue
        seen.add(uid)
        if isinstance(item, dict) and item.get("available") is True and item.get("source_quality") in {"original", "client_full"}:
            eligible.append((uid, item))
        else:
            reason = "image is unavailable" if not isinstance(item, dict) or item.get("available") is not True else "image quality is not original/client_full"
            output[uid] = {"status": "failed", "source_kind": "ocr", "reason": reason}
    if duplicates:
        eligible = [(uid, item) for uid, item in eligible if uid not in duplicates]
    work: list[tuple[str, dict[str, Any], bytes, str, Path]] = []
    for uid, item in eligible:
        try:
            data, _ = _image_bytes(item)
            image_sha = hashlib.sha256(data).hexdigest()
            cache_path = _cache_path(cache_dir, image_sha, data)
            cached = _valid_cached(cache_path, image_sha)
            if cached is not None:
                result = {key: cached[key] for key in ("text", "lines", "confidence", "low_confidence") if key in cached}
                output[uid] = {"status": "ok", "source_kind": "ocr", "image_sha256": image_sha, **result,
                               "engine": ENGINE, "engine_version": ENGINE_VERSION, "cache_hit": True}
            elif max_images and len(work) >= max_images:
                output[uid] = {'status': 'pending', 'source_kind': 'ocr', 'reason': 'max_images limit reached'}
            else:
                work.append((uid, item, None, image_sha, cache_path))
        except Exception as exc:
            output[uid] = {"status": "failed", "source_kind": "ocr",
                           "reason": f"{type(exc).__name__}: {exc}"}
    if work:
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"
        os.environ["NUMEXPR_NUM_THREADS"] = "1"
        try:
            ocr = _create_ocr()
        except Exception as exc:
            message = f"OCR dependency/model initialization failed: {type(exc).__name__}: {exc}"
            for uid, _, _, _, _ in work:
                output[uid] = {"status": "failed", "source_kind": "ocr", "reason": message}
            ocr = None
        if ocr is not None:
            for uid, item, data, image_sha, cache_path in work:
                try:
                    data, _ = _image_bytes(item)
                    text, lines, confidence = _recognize(data, ocr)
                    low_confidence = confidence is None or confidence < 0.75
                    record = {"schema": CACHE_SCHEMA, "image_sha256": image_sha, "engine": ENGINE,
                              "engine_version": ENGINE_VERSION, "text": text, "lines": lines,
                              "confidence": confidence, "low_confidence": low_confidence}
                    _private_json(cache_path, record)
                    output[uid] = {"status": "ok", "source_kind": "ocr", "image_sha256": image_sha,
                                   "text": text, "lines": lines, "confidence": confidence,
                                   "engine": ENGINE, "engine_version": ENGINE_VERSION,
                                   "low_confidence": low_confidence, "cache_hit": False}
                except Exception as exc:
                    output[uid] = {"status": "failed", "source_kind": "ocr",
                                   "reason": f"{type(exc).__name__}: {exc}"}
    _private_json(output_path, output)
    counts = {status: sum(value.get("status") == status for value in output.values()) for status in ("ok", "failed", "pending")}
    return {"messages": len(manifest), **counts, "output": str(output_path), "engine": ENGINE, "engine_version": ENGINE_VERSION}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("/work/ocr-cache"))
    parser.add_argument("--max-images", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, list):
            raise ValueError("image manifest must be a JSON list")
        stats = process_manifest(manifest, args.output, args.cache_dir, args.max_images)
        print(json.dumps(stats, ensure_ascii=False), flush=True)
        return 0 if stats["failed"] == 0 and stats["pending"] == 0 else 2
    except Exception as exc:
        print(json.dumps({"messages": 0, "failed": 1, "pending": 0, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
