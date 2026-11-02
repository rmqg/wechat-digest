"""Synchronous OneBot v11 WebSocket client for a single QQ group.

Public API: ``QQClient(url, token)`` is a context manager; ``rpc(action, params)``
performs one echo-correlated API call; ``history(group_id, start, end)`` returns
normalized messages in the half-open Unix-time interval [start, end);
``send_image(group_id, path)`` sends a PNG and verifies its receipt;
``send_text(group_id, text)`` sends and verifies exact text. ``normalize_messages``
converts raw OneBot messages to the shared digest schema.

Failures raise ``QQError`` (including API/validation failures),
``HistoryIncompleteError`` when history pagination cannot establish coverage,
or ``DeliveryUnknownError`` when a send's outcome cannot safely be determined.
Send failures are never retried automatically.
"""
from __future__ import annotations

import base64
import itertools
import json
from pathlib import Path
import threading
from typing import Any, Iterable
import time

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect


class QQError(RuntimeError):
    """Base error for QQ OneBot operations."""


class RPCError(QQError):
    """The OneBot endpoint returned an invalid response or nonzero retcode."""


class HistoryIncompleteError(QQError):
    """History pagination did not establish complete coverage of the interval."""


class DeliveryUnknownError(QQError):
    """A send may have reached QQ, but its delivery receipt was not verified."""


def _message_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        messages = raw
    elif isinstance(raw, dict):
        messages = raw.get("messages")
    else:
        messages = None
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise HistoryIncompleteError("history response has no valid messages list")
    return messages


def normalize_messages(raw: Iterable[dict[str, Any]], group_id: int) -> list[dict[str, Any]]:
    """Normalize OneBot messages, refusing messages not proven to belong to group_id."""
    normalized = []
    for message in raw:
        if not isinstance(message, dict):
            raise ValueError("OneBot message must be an object")
        try:
            actual_group = int(message["group_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("OneBot message is missing a valid group_id") from exc
        if actual_group != int(group_id):
            raise ValueError("OneBot message belongs to a different group")

        sender = message.get("sender")
        if not isinstance(sender, dict) or sender.get("user_id") is None:
            raise ValueError("OneBot message is missing sender.user_id")
        author_id = str(sender["user_id"])
        author = sender.get("card") or sender.get("nickname") or author_id
        segments = message.get("message", message.get("content", []))
        if isinstance(segments, str):
            segments = [{"type": "text", "data": {"text": segments}}]
        if not isinstance(segments, list):
            raise ValueError("OneBot message segments must be a list")

        texts: list[str] = []
        image_items: list[dict[str, Any]] = []
        at_ids = []
        references: list[dict[str, Any]] = []
        kinds: set[str] = set()
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("OneBot segment must be an object")
            kind = segment.get("type")
            data = segment.get("data", {})
            if not isinstance(data, dict):
                raise ValueError("OneBot segment data must be an object")
            if kind == "text":
                text = data.get("text", "")
                if not isinstance(text, str):
                    raise ValueError("OneBot text segment must contain string text")
                texts.append(text)
                if text:
                    kinds.add("text")
            elif kind == 'at':
                target = str(data.get('qq', ''))
                if target:
                    if target.isdecimal():
                        at_ids.append(target)
                    texts.append('@' + target)
                    kinds.add('text')
            elif kind == "image":
                image_items.append(dict(data))
                kinds.add("image")
            elif kind in ("reply", "forward"):
                # Retain the original segment, but never execute or resolve it.
                references.append({"type": kind, "data": dict(data)})
                kinds.add(kind)
        timestamp = message.get("time")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValueError("OneBot message is missing a numeric timestamp")
        message_uid = message.get("message_id")
        if message_uid is None:
            message_uid = message.get("message_seq")
        if message_uid is None:
            raise ValueError("OneBot message is missing message_id/message_seq")
        normalized.append({
            "message_uid": f"qq:{group_id}:{message_uid}",
            "reference": f"Q{message_uid}",
            "reply_references": references,
            "_at_ids": at_ids,
            "timestamp": timestamp,
            "author_id": author_id,
            "author": str(author),
            "type": 'image' if 'image' in kinds else ('text' if 'text' in kinds else '+'.join(sorted(kinds)) or 'unknown'),
            "text": "".join(texts),
            "image_items": image_items,
        })
    return normalized


class QQClient:
    """OneBot WebSocket client; owns at most one synchronous WebSocket connection."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0):
        if not url or not isinstance(url, str):
            raise ValueError("url must be a nonempty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a nonempty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url = url
        self.token = token
        self.timeout = timeout
        self._ws = None
        self._echoes = itertools.count(1)
        self._lock = threading.Lock()
        self._self_id: str | None = None

    def __enter__(self) -> "QQClient":
        if self._ws is not None:
            raise QQError("QQClient is already connected")
        self._ws = connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                           open_timeout=self.timeout, close_timeout=self.timeout)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()

    def rpc(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Call one OneBot action, correlating its response by echo and checking retcode."""
        if self._ws is None:
            raise QQError("QQClient is not connected")
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a nonempty string")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a dictionary")
        echo = str(next(self._echoes))
        request = {"action": action, "params": params or {}, "echo": echo}
        with self._lock:
            try:
                self._ws.send(json.dumps(request, ensure_ascii=False))
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('OneBot RPC deadline exceeded')
                    response = json.loads(self._ws.recv(timeout=remaining))
                    if not isinstance(response, dict):
                        raise RPCError("OneBot response is not an object")
                    if str(response.get("echo")) != echo:
                        continue
                    if ("retcode" not in response or not isinstance(response["retcode"], int)
                            or isinstance(response["retcode"], bool)):
                        raise RPCError("OneBot response has no integer retcode")
                    if response["retcode"] != 0:
                        raise RPCError(f"OneBot {action} returned retcode {response['retcode']}")
                    return response.get("data")
            except RPCError:
                raise
            except (TimeoutError, ConnectionClosed, WebSocketException, json.JSONDecodeError, TypeError) as exc:
                raise RPCError(f"OneBot {action} failed: {type(exc).__name__}") from exc

    def history(self, group_id: int, start: int | float, end: int | float) -> list[dict[str, Any]]:
        """Fetch history in [start, end), raising if sequence pagination stalls or is malformed."""
        group_id = _valid_group_id(group_id)
        if start >= end:
            raise ValueError('invalid history window')
        cursor = None
        selected = []
        seen_sequences = set()
        seen_uid = set()
        while True:
            params: dict[str, Any] = {"group_id": group_id, "count": 100, "reverseOrder": True}
            if cursor is not None:
                params["message_seq"] = cursor
            data = self.rpc("get_group_msg_history", params)
            page = _message_list(data)
            if not page:
                break
            sequences = []
            for message in page:
                try:
                    actual_group = int(message["group_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise HistoryIncompleteError("history message missing valid group_id") from exc
                if actual_group != group_id:
                    raise HistoryIncompleteError("history message belongs to a different group")
                seq = message.get("message_seq")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    raise HistoryIncompleteError("history message missing integer message_seq")
                sequences.append(seq)
            new_sequences = set(sequences) - seen_sequences
            if not new_sequences:
                raise HistoryIncompleteError("history pagination made no sequence progress")
            seen_sequences.update(sequences)
            for message in page:
                timestamp = message.get("time")
                if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                    raise HistoryIncompleteError("history message missing numeric timestamp")
                uid = message.get("message_id", message["message_seq"])
                if start <= timestamp < end and str(uid) not in seen_uid:
                    seen_uid.add(str(uid))
                    selected.append(message)
            if min(message['time'] for message in page) <= start:
                break
            oldest_message = min(page, key=lambda message: (message['time'], int(message.get('real_seq', 0))))
            oldest = int(oldest_message['message_seq'])
            cursor = oldest
            if cursor == 0:
                raise HistoryIncompleteError("history returned unusable message_seq cursor")
        selected.sort(key=lambda item: (item['time'], int(item.get('real_seq', 0))))
        return normalize_messages(selected, group_id)

    def send_image(self, group_id: int, path: str | Path) -> Any:
        """Send the exact PNG bytes and verify the resulting group message as our image."""
        group_id = _valid_group_id(group_id)
        image_path = Path(path)
        if image_path.is_symlink() or not image_path.is_file():
            raise ValueError("image path must be a regular PNG file")
        payload = image_path.read_bytes()
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("image must be a PNG")
        encoded = "base64://" + base64.b64encode(payload).decode("ascii")
        message_id = self._send(group_id, [{'type': 'image', 'data': {'file': encoded}}], 'image')
        self._verify_sent(message_id, group_id, "image", expected_text=None)
        return message_id

    def send_text(self, group_id: int, text: str) -> Any:
        """Send text once and verify exact returned group/body before reporting success."""
        group_id = _valid_group_id(group_id)
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a nonempty string")
        message_id = self._send(group_id, text, "text")
        self._verify_sent(message_id, group_id, "text", expected_text=text)
        return message_id

    def _send(self, group_id: int, message: Any, kind: str) -> Any:
        try:
            data = self.rpc("send_group_msg", {"group_id": group_id, "message": message})
        except RPCError as exc:
            # The API call was transmitted; without a correlated acceptance response,
            # sending again could duplicate a delivered message.
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown") from exc
        if not isinstance(data, dict) or data.get("message_id") is None:
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: no message_id")
        return data["message_id"]

    def _verify_sent(self, message_id: Any, group_id: int, kind: str,
                     expected_text: str | None) -> None:
        try:
            if self._self_id is None:
                login = self.rpc("get_login_info", {})
                if not isinstance(login, dict) or login.get("user_id") is None:
                    raise RPCError("get_login_info omitted user_id")
                self._self_id = str(login["user_id"])
            data = self.rpc("get_msg", {"message_id": message_id})
        except (QQError, TimeoutError, ConnectionClosed, WebSocketException) as exc:
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: receipt unavailable") from exc
        if not isinstance(data, dict):
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: invalid receipt")
        if data.get("message_id") != message_id:
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: receipt id mismatch")
        try:
            normalized = normalize_messages([data], group_id)[0]
        except (ValueError, KeyError) as exc:
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: receipt mismatch") from exc
        sender = data.get("sender") or {}
        if str(sender.get("user_id")) != self._self_id or int(data.get("group_id", -1)) != group_id:
            raise DeliveryUnknownError(f"{kind} delivery outcome is unknown: receipt mismatch")
        if kind == "image":
            if not normalized["image_items"]:
                raise DeliveryUnknownError("image delivery outcome is unknown: receipt has no image")
        elif normalized["text"] != expected_text:
            raise DeliveryUnknownError("text delivery outcome is unknown: receipt text mismatch")


def _valid_group_id(group_id: int) -> int:
    if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
        raise ValueError("group_id must be a positive integer")
    return group_id
