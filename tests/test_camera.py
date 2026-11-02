"""Tests for DahuaArcPirCamera entities and snapshot serving."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from custom_components.dahua_arc.camera import DahuaArcPirCamera
from custom_components.dahua_arc.client import ArcHub, Zone
from custom_components.dahua_arc.protocol.files import (
    generate_placeholder_jpeg,
    is_jpeg,
)
from custom_components.dahua_arc.research.pircam import PirCamMedia


@pytest.fixture
def pircam_zone() -> Zone:
    z = Zone(index=0, name="Garage Hall PIRCam", sense_method="PIRCam")
    z.online_state = 1
    return z


@pytest.fixture
def fake_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {}
    return entry


def test_generate_placeholder_jpeg() -> None:
    jpeg = generate_placeholder_jpeg("Test PIRCam")
    assert is_jpeg(jpeg)
    assert len(jpeg) > 500


@pytest.mark.asyncio
async def test_camera_returns_placeholder_when_no_snapshot(
    pircam_zone: Zone, fake_entry: MagicMock
) -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "secret")
    hub.realtime = MagicMock(connected=True)
    hub.zones = {0: pircam_zone}
    hub.pircam = PirCamMedia(
        hub.host, 80, "admin", "secret", hub.zones, None, MagicMock()
    )
    cam = DahuaArcPirCamera(hub, fake_entry, pircam_zone)

    mock_hass = MagicMock()

    # Emulate async_add_executor_job executing synchronously in test
    async def run_executor(fn, *args):
        return fn(*args)

    mock_hass.async_add_executor_job = run_executor
    cam.hass = mock_hass

    img = await cam.async_camera_image()
    assert img is not None
    assert is_jpeg(img)
    assert cam.extra_state_attributes["has_snapshot"] is False
    assert cam.available is True


@pytest.mark.asyncio
async def test_camera_serves_actual_snapshot_when_present(
    pircam_zone: Zone, fake_entry: MagicMock
) -> None:
    hub = ArcHub("192.0.2.10", 80, 5000, "admin", "secret")
    hub.realtime = MagicMock(connected=True)
    hub.zones = {0: pircam_zone}
    pircam = PirCamMedia(hub.host, 80, "admin", "secret", hub.zones, None, MagicMock())
    real_jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    pircam._image_cache[0] = real_jpeg
    hub.pircam = pircam

    cam = DahuaArcPirCamera(hub, fake_entry, pircam_zone)

    mock_hass = MagicMock()

    async def run_executor(fn, *args):
        return fn(*args)

    mock_hass.async_add_executor_job = run_executor
    cam.hass = mock_hass

    img = await cam.async_camera_image()
    assert img == real_jpeg
    assert cam.extra_state_attributes["has_snapshot"] is True


def test_pircam_media_disk_caching(tmp_path: os.PathLike) -> None:
    zones = {0: Zone(index=0, name="Garage PIR", sense_method="PIRCam")}
    real_jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )

    # Instance 1 saves to disk cache
    pm1 = PirCamMedia(
        "192.0.2.10",
        80,
        "admin",
        "secret",
        zones,
        None,
        MagicMock(),
        storage_dir=str(tmp_path),
    )
    pm1._save_cached_image(0, real_jpeg)
    assert pm1.fetch_image(0) == real_jpeg

    # Instance 2 boots up and loads the cached file from disk
    pm2 = PirCamMedia(
        "192.0.2.10",
        80,
        "admin",
        "secret",
        zones,
        None,
        MagicMock(),
        storage_dir=str(tmp_path),
    )
    assert pm2.fetch_image(0) == real_jpeg
