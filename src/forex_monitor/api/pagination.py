"""Signed stable cursors for deterministic keyset pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, Optional, Sequence, Tuple, TypeVar

from forex_monitor.errors import ValidationError


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: Tuple[T, ...]
    next_cursor: Optional[str]
    previous_cursor: Optional[str]
    has_more: bool


class CursorPaginator(Generic[T]):
    def __init__(self, secret: str, *, maximum_page_size: int = 500) -> None:
        if not secret.strip():
            raise ValidationError("cursor secret is required")
        if maximum_page_size < 1 or maximum_page_size > 10000:
            raise ValidationError("maximum page size is invalid")
        self._secret = secret.encode("utf-8")
        self.maximum_page_size = maximum_page_size

    def paginate(
        self,
        items: Iterable[T],
        *,
        key: Callable[[T], Tuple[object, ...]],
        limit: int,
        cursor: Optional[str] = None,
        reverse: bool = False,
        scope: str = "default",
    ) -> Page[T]:
        if limit < 1 or limit > self.maximum_page_size:
            raise ValidationError("page limit is invalid")
        values = sorted(items, key=key, reverse=reverse)
        boundary = None
        direction = "reverse" if reverse else "forward"
        if cursor:
            payload = self.decode(cursor, expected_scope=scope)
            if payload["direction"] != direction:
                raise ValidationError("cursor direction does not match request")
            boundary = tuple(payload["key"])
        filtered = []
        for item in values:
            item_key = tuple(str(part) for part in key(item))
            if boundary is None or (item_key < boundary if reverse else item_key > boundary):
                filtered.append(item)
        selected = filtered[: limit + 1]
        has_more = len(selected) > limit
        page_items = tuple(selected[:limit])
        next_cursor = self.encode(tuple(str(part) for part in key(page_items[-1])), direction, scope) if page_items and has_more else None
        previous_cursor = self.encode(tuple(str(part) for part in key(page_items[0])), "forward" if reverse else "reverse", scope) if page_items and cursor else None
        return Page(page_items, next_cursor, previous_cursor, has_more)

    def encode(self, key: Tuple[str, ...], direction: str, scope: str) -> str:
        if direction not in {"forward", "reverse"}:
            raise ValidationError("cursor direction is invalid")
        payload = json.dumps({"key": key, "direction": direction, "scope": scope}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii").rstrip("=")

    def decode(self, cursor: str, *, expected_scope: str) -> dict:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload, signature = raw.rsplit(b".", 1)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValidationError("cursor signature is invalid")
            value = json.loads(payload.decode("utf-8"))
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("cursor is malformed") from exc
        if value.get("scope") != expected_scope or value.get("direction") not in {"forward", "reverse"} or not isinstance(value.get("key"), list):
            raise ValidationError("cursor does not match request scope")
        return value
