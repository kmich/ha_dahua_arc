from __future__ import annotations

import pytest_socket
from homeassistant.helpers import device_registry as dr

# On Windows, asyncio's ProactorEventLoop uses socket.socketpair() to create
# loopback pipes between threads/event loop. pytest-socket's disable_socket
# prevents all socket creation and breaks asyncio on Windows.
pytest_socket.disable_socket = lambda *args, **kwargs: None  # type: ignore[assignment]

if not hasattr(dr.DeviceRegistry, "async_get_device_by_identifier"):

    def _async_get_device_by_identifier(self, identifier, entry_id=None):  # type: ignore[no-untyped-def]
        return self.async_get_device(identifiers={identifier})

    dr.DeviceRegistry.async_get_device_by_identifier = _async_get_device_by_identifier  # type: ignore[attr-defined]

if not hasattr(dr.DeviceRegistry, "async_get_or_create_child"):

    def _async_get_or_create_child(
        self, *, config_entry_id, parent_device_id, identifiers, name, **kwargs
    ):  # type: ignore[no-untyped-def]
        kwargs["config_entry_id"] = config_entry_id
        kwargs["identifiers"] = identifiers
        kwargs["name"] = name
        kwargs["via_device_id"] = parent_device_id
        return self.async_get_or_create(**kwargs)

    dr.DeviceRegistry.async_get_or_create_child = _async_get_or_create_child  # type: ignore[attr-defined]

if not hasattr(dr.DeviceRegistry, "async_update_child_device"):
    dr.DeviceRegistry.async_update_child_device = dr.DeviceRegistry.async_update_device  # type: ignore[attr-defined]

if not hasattr(dr.DeviceEntry, "parent_device_id"):
    dr.DeviceEntry.parent_device_id = property(lambda self: self.via_device_id)  # type: ignore[attr-defined]

_orig_async_get_or_create = dr.DeviceRegistry.async_get_or_create


def _compat_async_get_or_create(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    via_device_id = kwargs.pop("via_device_id", None) or kwargs.pop(
        "parent_device_id", None
    )
    device = _orig_async_get_or_create(self, *args, **kwargs)
    if via_device_id is not None:
        device = self.async_update_device(device.id, via_device_id=via_device_id)
    return device


dr.DeviceRegistry.async_get_or_create = _compat_async_get_or_create  # type: ignore[assignment]
