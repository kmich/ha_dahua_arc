"""ARD1731 PIRCam snapshot/media tracking (research mode only)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..protocol.cgi import digest_opener
from ..protocol.engine import PIRCAM_EVENT_CODE
from ..protocol.files import extract_embedded_jpeg, is_jpeg
from ..protocol.models import Zone
from ..protocol.snapshot import SnapshotClient
from ..protocol.util import safe_int, timestamp

MAX_HTTP_IMAGE_BYTES = 5 * 1024 * 1024


class PirCamMedia:
    """Track PIRCam media events and fetch the latest still image locally."""

    def __init__(
        self,
        host: str,
        http_port: int,
        username: str,
        password: str,
        zones: dict[int, Zone],
        snapshot_client: SnapshotClient | None,
        notify: Callable[[set[int] | None], None],
    ) -> None:
        self.host, self.http_port = host, http_port
        self.username, self.password = username, password
        self.zones = zones
        self.snapshot_client = snapshot_client
        self._notify = notify
        self.lock = threading.RLock()
        # Latest PIR-camera snapshot metadata, keyed by Alarm[] index.
        # Populated from ManualTest / SpecialFileDelayUpload events.
        self.snapshots: dict[int, dict[str, Any]] = {}
        # Last successfully retrieved JPEG, kept in memory because ARC
        # /var/tmp files are short-lived.
        self._image_cache: dict[int, bytes] = {}

    def process_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self._process_event_locked(event)

    def _process_event_locked(self, event: dict[str, Any]) -> None:
        """Track ARD1731 snapshot and armed-alarm media lifecycles.

        Manual Snapshot Test:
            ManualTest -> JPEG -> SpecialFileDelayUpload

        Real armed PIR intrusion:
            AlarmLocal Start/Stop -> MP4 -> SpecialFileDelayUpload

        The two media flows are deliberately kept separate so an alarm video
        never replaces/corrupts the Home Assistant still-image camera entity.
        """
        code = str(event.get("Code") or "")
        data = event.get("Data") or {}
        if not isinstance(data, dict):
            return

        if code == "ManualTest" and str(data.get("DevType") or "") == "PIRCam":
            idx = safe_int(event.get("Index"))
            if idx is None:
                return
            entry = self.snapshots.setdefault(idx, {})
            entry.update(
                {
                    "index": idx,
                    "name": str(
                        data.get("Name")
                        or self.zones.get(idx, Zone(idx)).name
                        or f"PIRCam {idx}"
                    ),
                    "model": str(data.get("Model") or ""),
                    "alarm_id": str(data.get("AlarmId") or ""),
                    "delay_upload_seq": str(data.get("DelayUploadSeq") or ""),
                    "file_path": str(data.get("FilePath") or ""),
                    "picture_count": safe_int(
                        (data.get("FilesInfo") or {}).get("PictureCount")
                    ),
                    "captured_at": str(data.get("LocaleTime") or timestamp()),
                    "last_event": code,
                    "media_kind": "snapshot_jpeg",
                    # Clear transient transfer metadata from the prior snapshot.
                    "temp_path": "",
                    "expected_length": None,
                    "unique_id": "",
                    "upload_ready_at": None,
                }
            )
            self._notify({idx})
            return

        if (
            code == PIRCAM_EVENT_CODE
            and event.get("Action") == "Start"
            and str(data.get("DevType") or data.get("SenseMethod") or "") == "PIRCam"
        ):
            idx = safe_int(event.get("Index"))
            if idx is None:
                return
            entry = self.snapshots.setdefault(idx, {})
            entry.update(
                {
                    "index": idx,
                    "name": str(
                        data.get("Name")
                        or self.zones.get(idx, Zone(idx)).name
                        or f"PIRCam {idx}"
                    ),
                    "model": str(data.get("Model") or ""),
                    "last_alarm_id": str(data.get("AlarmId") or ""),
                    "last_alarm_at": str(data.get("LocaleTime") or timestamp()),
                    "last_alarm_media_kind": (
                        "video_mp4"
                        if safe_int(data.get("FileType")) == 2
                        else "unknown"
                    ),
                    "last_alarm_file_path": str(data.get("FilePath") or ""),
                    "last_alarm_file_type": safe_int(data.get("FileType")),
                    "last_alarm_video_count": safe_int(
                        (data.get("FilesInfo") or {}).get("VideoCount")
                    ),
                    "last_alarm_delay_upload_seq": str(
                        data.get("DelayUploadSeq") or ""
                    ),
                    "last_alarm_event": code,
                }
            )
            self._notify({idx})
            return

        if code == "SpecialFileDelayUpload":
            upload_seq = str(data.get("UploadSeq") or "")
            files = data.get("Files") or []
            if not upload_seq or not isinstance(files, list):
                return
            first = files[0] if files and isinstance(files[0], dict) else {}

            for idx, entry in self.snapshots.items():
                if entry.get("delay_upload_seq") == upload_seq:
                    # Snapshot Test JPEG transfer.
                    entry.update(
                        {
                            "temp_path": str(first.get("FilePath") or ""),
                            "expected_length": safe_int(first.get("Length")),
                            "unique_id": str(first.get("UniqueID") or upload_seq),
                            "upload_ready_at": str(
                                data.get("LocaleTime") or timestamp()
                            ),
                            "last_event": code,
                        }
                    )
                    self._notify({idx})
                    return

                if entry.get("last_alarm_delay_upload_seq") == upload_seq:
                    # Armed intrusion media transfer. Current ARD1731 firmware
                    # reports this as a short MP4, not a JPEG snapshot.
                    entry.update(
                        {
                            "last_alarm_temp_path": str(first.get("FilePath") or ""),
                            "last_alarm_expected_length": safe_int(first.get("Length")),
                            "last_alarm_unique_id": str(
                                first.get("UniqueID") or upload_seq
                            ),
                            "last_alarm_upload_ready_at": str(
                                data.get("LocaleTime") or timestamp()
                            ),
                            "last_alarm_transfer_format": safe_int(first.get("Format")),
                        }
                    )
                    self._notify({idx})
                    return

    def snapshot(self, index: int) -> dict[str, Any] | None:
        with self.lock:
            item = self.snapshots.get(index)
            return dict(item) if item else None

    def diagnostics(self) -> dict[int, dict[str, Any]]:
        with self.lock:
            return {idx: dict(meta) for idx, meta in sorted(self.snapshots.items())}

    def fetch_image(self, index: int) -> bytes | None:
        """Fetch the latest PIR-camera JPEG locally from the ARC.

        Preferred path: DHIP FileManager.downloadFile using the exact file
        paths reported by ManualTest / SpecialFileDelayUpload.
        HTTP is retained only as a fallback for firmware variants that expose
        the persistent backup path directly.
        """
        item = self.snapshots.get(index)
        if not item:
            return None

        expected = safe_int(item.get("expected_length"))
        candidates: list[tuple[str, str]] = []
        temp_path = str(item.get("temp_path") or "")
        file_path = str(item.get("file_path") or "")
        if temp_path.startswith("/"):
            candidates.append(("dhip-temp", temp_path))
        if file_path.startswith("/") and file_path != temp_path:
            candidates.append(("dhip-backup", file_path))

        errors: list[str] = []

        # Primary method: Dahua's local DHIP FileManager.downloadFile.
        if self.snapshot_client is not None:
            for method_name, remote_path in candidates:
                try:
                    raw = self.snapshot_client.download_file(
                        remote_path,
                        expected_length=expected,
                        timeout=20.0,
                    )
                    if not raw:
                        errors.append(f"{method_name}: empty response")
                        continue

                    jpeg, payload_debug = extract_embedded_jpeg(raw)
                    item["last_payload_debug"] = payload_debug
                    if jpeg is None:
                        errors.append(
                            f"{method_name}: no complete JPEG in payload "
                            f"({len(raw)} bytes; SOI={payload_debug.get('jpeg_soi_offset')}; "
                            f"EOI={payload_debug.get('jpeg_eoi_offset')})"
                        )
                        continue
                    raw = jpeg

                    item["last_fetch_method"] = method_name
                    item["last_fetch_path"] = remote_path
                    item["last_fetch_bytes"] = len(raw)
                    item["last_fetch_at"] = timestamp()
                    item["last_fetch_error"] = None
                    item["last_fetch_fallback"] = None
                    self._image_cache[index] = raw
                    if expected and expected != len(raw):
                        item["length_mismatch"] = f"expected={expected}, got={len(raw)}"
                    else:
                        item["length_mismatch"] = None
                    return raw
                except Exception as exc:
                    errors.append(f"{method_name}: {type(exc).__name__}: {exc}")

        # Fallback: exact persistent path over HTTP Digest. Some Dahua models
        # expose it directly, while others require FileManager.downloadFile.
        if file_path.startswith("/"):
            url = f"http://{self.host}:{self.http_port}{file_path}"
            opener = digest_opener(
                self.host, self.http_port, self.username, self.password
            )
            try:
                with opener.open(url, timeout=12) as response:
                    raw = response.read(MAX_HTTP_IMAGE_BYTES)
                if is_jpeg(raw):
                    item["last_fetch_method"] = "http-backup"
                    item["last_fetch_path"] = file_path
                    item["last_fetch_bytes"] = len(raw)
                    item["last_fetch_at"] = timestamp()
                    item["last_fetch_error"] = None
                    item["last_fetch_fallback"] = None
                    self._image_cache[index] = raw
                    item["length_mismatch"] = (
                        f"expected={expected}, got={len(raw)}"
                        if expected and expected != len(raw)
                        else None
                    )
                    return raw
                errors.append(f"http-backup: non-JPEG payload ({len(raw)} bytes)")
            except Exception as exc:
                errors.append(f"http-backup: {type(exc).__name__}: {exc}")

        item["last_fetch_at"] = timestamp()
        item["last_fetch_bytes"] = None
        item["last_fetch_error"] = (
            " | ".join(errors) or "No usable PIR-camera file path"
        )

        cached = self._image_cache.get(index)
        if cached:
            item["last_fetch_fallback"] = "memory-cache"
            item["last_fetch_bytes"] = len(cached)
            return cached

        item["last_fetch_fallback"] = None
        return None
