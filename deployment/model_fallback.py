"""Ordered model providers and conservative fallback policy for report tasks."""
from __future__ import annotations

import hashlib
import json
import urllib.error


def providers(config: dict) -> list[dict]:
    if not isinstance(config, dict):
        raise ValueError('model config must be an object')
    fallbacks = config.get('fallbacks', [])
    if not isinstance(fallbacks, list):
        raise ValueError('fallbacks must be a list')
    primary = {k: v for k, v in config.items() if k != 'fallbacks'}
    result, seen = [], set()
    for candidate in [primary, *fallbacks]:
        if not isinstance(candidate, dict):
            raise ValueError('model provider must be an object')
        for key in ('base_url', 'model', 'api_key'):
            if not isinstance(candidate.get(key), str) or not candidate[key].strip():
                raise ValueError('model provider requires non-empty ' + key)
        provider = {**primary, **candidate}
        provider.pop('fallbacks', None)
        signature = provider_key(provider)
        if signature not in seen:
            seen.add(signature)
            result.append(provider)
    return result


def identity(provider: dict) -> dict:
    """Non-secret configured identity; response model IDs may differ."""
    return {'base_url': provider['base_url'].rstrip('/'), 'model': provider['model']}


def provider_key(provider: dict) -> str:
    value = [identity(provider), hashlib.sha256(provider['api_key'].encode()).hexdigest()]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def is_fallback_error(error: BaseException) -> bool:
    """Never switch providers to mask malformed requests or safety refusals."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (401, 408, 425, 429, 500, 502, 503, 504, 529)
    return isinstance(error, (urllib.error.URLError, TimeoutError, OSError))
