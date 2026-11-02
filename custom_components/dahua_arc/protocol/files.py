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
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None, payload
                if not isinstance(obj, dict):
                    return None, payload
                return obj, payload[end:]
    return None, payload


# 320x240 solid dark slate baseline JPEG fallback in case PIL is unavailable.
_FALLBACK_PLACEHOLDER_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c"
    b"\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c"
    b"\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c"
    b"\x0b\x0c\x18\r\r\x182!\x1c!2222222222222222222222222222222222222222"
    b'2222222222\xff\xc0\x00\x11\x08\x00\xf0\x01@\x03\x01"\x00\x02\x11\x01'
    b"\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n"
    b"\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04"
    b'\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q'
    b"\x142\x81\x91\xa1\x08#B\xb1\xc1\x15$3br\x82\xd1\xf0\x24\x43\xe1\xf1\t"
    b"\x16\x25\x52\x83\x92\xa2\xb2\xc2\xff\xc4\x00\x1f\x01\x00\x03\x01\x01"
    b"\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04"
    b"\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04"
    b"\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x12"
    b'AQaq\x13"2\x81\x06\x14\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\x24\x42'
    b"\xe1\xf1\x16$4CSs\x82\x83\xa2\xb2\xc2\xff\xda\x00\x0c\x03\x01\x00\x02"
    b"\x11\x03\x11\x00?\x00\xfd\xec\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2"
    b"\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00"
    b"\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a"
    b"\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2\x8a\x00\xa2"
    b"\x8a\x00\xa2\x8a\x00\xff\xd9"
)


def generate_placeholder_jpeg(label: str = "PIR Camera") -> bytes:
    """Generate a clean JPEG placeholder when no snapshot has been captured yet.

    Uses Pillow when present to render a styled dark slate graphic with a camera
    icon and clear status text. If Pillow is unavailable or fails, returns a
    static minimal JPEG so the camera image view never fails with 500 or 0 bytes.
    """
    try:
        import io

        from PIL import Image, ImageDraw

        width, height = 640, 480
        img = Image.new("RGB", (width, height), color=(24, 30, 42))
        draw = ImageDraw.Draw(img)

        # Subtle border
        draw.rectangle([10, 10, width - 11, height - 11], outline=(51, 65, 85), width=2)

        # Center camera icon
        cx, cy = width // 2, height // 2 - 40
        draw.rounded_rectangle(
            [cx - 60, cy - 35, cx + 60, cy + 45],
            radius=10,
            fill=(30, 41, 59),
            outline=(71, 85, 105),
            width=3,
        )
        draw.rounded_rectangle(
            [cx - 20, cy - 50, cx + 20, cy - 33],
            radius=4,
            fill=(40, 53, 72),
            outline=(71, 85, 105),
            width=2,
        )
        draw.ellipse(
            [cx - 28, cy - 18, cx + 28, cy + 38],
            fill=(15, 23, 42),
            outline=(99, 102, 241),
            width=4,
        )
        draw.ellipse(
            [cx - 14, cy - 4, cx + 14, cy + 24],
            fill=(30, 41, 59),
            outline=(129, 140, 248),
            width=2,
        )
        draw.ellipse([cx + 2, cy - 1, cx + 8, cy + 5], fill=(224, 231, 255))

        # Typography
        lines = [
            (label, 70, (241, 245, 249)),
            ("No snapshot recorded yet", 95, (148, 163, 184)),
            ("Awaiting armed intrusion or snapshot test", 120, (100, 116, 139)),
        ]
        for text, y_off, color in lines:
            bb = draw.textbbox((0, 0), text)
            tw = bb[2] - bb[0]
            draw.text((cx - tw // 2, cy + y_off), text, fill=color)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return _FALLBACK_PLACEHOLDER_JPEG
