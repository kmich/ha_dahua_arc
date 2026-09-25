"""Pure helpers for parsing Dahua FileManager.downloadFile payloads."""

from __future__ import annotations

import json
from typing import Any

from .util import safe_int

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

_LENGTH_FIELDS = (
    "length",
    "fileLength",
    "fileSize",
    "size",
    "totalLength",
    "totalSize",
    "dataLen",
    "dataSize",
)


def is_jpeg(raw: bytes) -> bool:
    return len(raw) >= 4 and raw[:2] == JPEG_SOI


def extract_embedded_jpeg(raw: bytes) -> tuple[bytes | None, dict[str, Any]]:
    """Find and extract a complete JPEG embedded in a Dahua file payload."""
    info: dict[str, Any] = {
        "raw_bytes": len(raw),
        "head_hex": raw[:32].hex(),
        "tail_hex": raw[-32:].hex() if raw else "",
    }
    # JPEG SOI normally begins ff d8 ff, but accept ff d8 as well.
    soi = raw.find(JPEG_SOI + b"\xff")
    if soi < 0:
        soi = raw.find(JPEG_SOI)
    info["jpeg_soi_offset"] = soi if soi >= 0 else None
    if soi < 0:
        return None, info

    eoi = raw.find(JPEG_EOI, soi + 2)
    info["jpeg_eoi_offset"] = eoi if eoi >= 0 else None
    if eoi < 0:
        return None, info

    jpeg = raw[soi : eoi + 2]
    info["jpeg_bytes"] = len(jpeg)
    info["prefix_bytes"] = soi
    info["suffix_bytes"] = len(raw) - (eoi + 2)
    return jpeg, info


def extract_download_length(obj: dict[str, Any]) -> int | None:
    """Extract a file length from common Dahua download response fields."""

    def scan(value: Any) -> int | None:
        if not isinstance(value, dict):
            return None
        for key in _LENGTH_FIELDS:
            n = safe_int(value.get(key))
            if n is not None and n > 0:
                return n
        return None

    for candidate in (obj, obj.get("params"), obj.get("result")):
        length = scan(candidate)
        if length is not None:
            return length
    return None


def split_json_prefix(payload: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Split a leading JSON object from a raw DHIP payload if present."""
    stripped = payload.lstrip()
    offset = len(payload) - len(stripped)
    if not stripped.startswith(b"{"):
        return None, payload

    depth = 0
    in_string = False
    escaped = False
    for i, byte in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte == 0x7B:  # {
            depth += 1
        elif byte == 0x7D:  # }
            depth -= 1
            if depth == 0:
                end = offset + i + 1
                try:
                    obj = json.loads(payload[offset:end].decode("utf-8"))
                except UnicodeDecodeError, json.JSONDecodeError:
                    return None, payload
                if not isinstance(obj, dict):
                    return None, payload
                return obj, payload[end:]
    return None, payload
