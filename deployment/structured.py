"""Strict JSON-object parsing with optional Markdown code-block framing."""
import json
import time
import http.client


def parse_object(content):
    if not isinstance(content, str):
        raise ValueError("structured response must be text")
    text = content.strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].lower() in {"```json", "```"} and lines[-1] == "```":
        text = "\n".join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("structured response must be an object")
    return value


class CompletionReadError(ValueError):
    def __init__(self, category, response):
        super().__init__(category)
        self.category = category
        self.response = response


def read_completion(response, deadline_seconds=900):
    """Collect a complete OpenAI-compatible JSON or SSE response, never partial prose."""
    headers = getattr(response, 'headers', {})
    if 'text/event-stream' not in str(headers.get('Content-Type', '')).lower():
        raw = bytearray()
        started = time.monotonic()
        read = getattr(response, 'read1', response.read)
        try:
            while True:
                if time.monotonic() - started > deadline_seconds:
                    raise CompletionReadError('response_deadline', {'received_bytes': len(raw)})
                chunk = read(65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 16 * 1024 * 1024:
                    raise CompletionReadError('response_too_large', {'received_bytes': len(raw)})
            return json.loads(raw.decode('utf-8-sig'))
        except CompletionReadError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise CompletionReadError('stream_transport', {'received_bytes': len(raw)}) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise CompletionReadError('malformed_response', {'raw_excerpt': raw[:8192].decode('utf-8', 'replace')}) from exc
    started = time.monotonic()
    fragments, event_lines = [], []
    finish_reason = None
    model, usage, refusal = None, None, None
    characters = 0
    event_bytes = 0

    def snapshot():
        return {'model': model, 'usage': usage, 'response_transport': 'sse',
                'choices': [{'finish_reason': finish_reason,
                             'message': {'content': ''.join(fragments), 'refusal': refusal}}]}

    def consume(lines):
        nonlocal finish_reason, model, usage, refusal, characters
        data = '\n'.join(lines)
        if data == '[DONE]':
            return True
        event = json.loads(data)
        if not isinstance(event, dict) or event.get('error'):
            raise ValueError('invalid completion event')
        model = event.get('model') or model
        usage = event.get('usage') or usage
        choices = event.get('choices') or []
        if choices:
            choice = choices[0]
            delta = choice.get('delta') or choice.get('message') or {}
            content = delta.get('content')
            if isinstance(content, list):
                content = ''.join(part['text'] for part in content if isinstance(part, dict) and isinstance(part.get('text'), str))
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError('invalid completion text')
                fragments.append(content)
                characters += len(content)
                if characters > 8 * 1024 * 1024:
                    raise ValueError('completion content exceeds bound')
            refusal = delta.get('refusal') or refusal
            finish_reason = choice.get('finish_reason') or finish_reason
        return False

    try:
        while True:
            if time.monotonic() - started > deadline_seconds:
                raise CompletionReadError('stream_deadline', snapshot())
            raw = response.readline(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('completion event exceeds bound')
            if not raw:
                if event_lines:
                    consume(event_lines)
                break
            line = raw.decode('utf-8-sig').rstrip('\r\n')
            if not line:
                if event_lines:
                    done = consume(event_lines)
                    event_lines = []
                    event_bytes = 0
                    if done:
                        break
            elif line.startswith('data:'):
                event_lines.append(line[5:].lstrip(' '))
                event_bytes += len(raw)
                if event_bytes > 8 * 1024 * 1024:
                    raise ValueError('completion event exceeds bound')
    except CompletionReadError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise CompletionReadError('stream_transport', snapshot()) from exc
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise CompletionReadError('malformed_stream', snapshot()) from exc
    if not finish_reason:
        raise CompletionReadError('stream_missing_finish', snapshot())
    return snapshot()
