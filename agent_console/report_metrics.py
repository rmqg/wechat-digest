"""Displayed body count and compact report statistics; raw metadata stays unchanged."""
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def _o200k_encoding():
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError("tiktoken is required for speaker token rankings") from exc

    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR")
    if not cache_dir:
        raise RuntimeError("TIKTOKEN_CACHE_DIR must point to a prewarmed token cache")
    url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    cache_key = hashlib.sha1(url.encode()).hexdigest()
    cache_file = Path(cache_dir) / cache_key
    try:
        with cache_file.open("rb") as source:
            cache_hash = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise RuntimeError("prewarmed token data cannot be read from TIKTOKEN_CACHE_DIR") from exc
    if cache_hash != "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d":
        raise RuntimeError("prewarmed o200k token data failed its integrity check")
    return tiktoken.get_encoding("o200k_base")

_AUTO_MEDIA_PLACEHOLDERS = {
    "image": re.compile(r"\s*(?:\[图片\]|【图片】)\s*"),
    "sticker": re.compile(r"\s*(?:\[表情\]|【表情】|\[动画表情\]|【动画表情】)\s*"),
    "video": re.compile(r"\s*(?:\[视频\]|【视频】)\s*"),
    "voice": re.compile(r"\s*(?:\[语音\]|【语音】)\s*"),
    "link_or_file": re.compile(r"\s*(?:\[链接/文件\]|【链接/文件】)\s*"),
}


def body_token_count(value: str) -> int:
    """Count the literal string exactly, treating special-token spellings as ordinary text."""
    return len(_o200k_encoding().encode_ordinary(str(value or "")))


def message_body_token_count(value: str, message_type: str) -> int:
    """Count readable message text, except a type's whole automatic media placeholder."""
    text = str(value or "")
    if message_type in {"system", "recall"}:
        return 0
    placeholder = _AUTO_MEDIA_PLACEHOLDERS.get(str(message_type or ""))
    if placeholder and placeholder.fullmatch(text):
        return 0
    return body_token_count(text)

_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ee5f\U0002f800-\U0002fa1f\U00030000-\U0003347f]')


def body_chinese_count(report: dict) -> int:
    parts = [report.get('one_line') or '']
    for topic in report.get('topics') or []:
        parts.extend((topic.get('topic') or '', topic.get('summary') or ''))
        details = topic.get('details')
        parts.extend(details if isinstance(details, list) else topic.get('samples') or [])
    parts.extend(quote.get('text') or '' for quote in report.get('quotes') or [])
    return sum(1 for part in parts for _ in _HAN.finditer(str(part)))


def peak_time_text(report: dict) -> str:
    name = str(((report.get('leaders') or {}).get('peak') or {}).get('name') or '—')
    return re.sub(r'^\d{4}-(?=\d{2}-\d{2}(?:\s|$))', '', name)
