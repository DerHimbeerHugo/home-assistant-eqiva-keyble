"""Diagnostics support for the Eqiva Key-BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_USER_ID, CONF_USER_KEY
from .ha_gatt_transport import HomeAssistantGattTransport
from .raw_att_transport import RawAttTransport, local_raw_path

_TO_REDACT = [CONF_ADDRESS, CONF_USER_ID, CONF_USER_KEY]


def _status_diagnostics(status: Any) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "lock_status": status.lock_status,
        "is_locked": status.is_locked,
        "is_moving": status.is_moving,
        "battery_low": status.battery_low,
        "pairing_allowed": status.pairing_allowed,
    }


def _transport_diagnostics(transport: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": str(transport.kind),
        "connected": transport.is_connected,
    }

    if isinstance(transport, HomeAssistantGattTransport):
        backend = getattr(transport, "_backend_name", None)
        backend_lower = (backend or "").lower()
        if "bleak_esphome" in backend_lower:
            path_type = "esphome_proxy"
        elif "bluezdbus" in backend_lower:
            path_type = "local_bluez"
        else:
            path_type = "unknown"
        data.update(
            {
                "path_type": path_type,
                "backend": backend,
                "rssi": getattr(transport, "_rssi", None),
                "notify_mode": getattr(transport, "_notify_mode", None),
            }
        )
        return data

    if isinstance(transport, RawAttTransport):
        path = local_raw_path(transport.hass, transport.address)
        if path is None:
            data.update(
                {
                    "path_type": "local_raw_att",
                    "local_path_available": False,
                }
            )
            return data
        scanner = path.scanner
        data.update(
            {
                "path_type": "local_raw_att",
                "local_path_available": True,
                "adapter": scanner.adapter,
                "adapter_idx": scanner.adapter_idx,
                "rssi": path.advertisement.rssi,
            }
        )
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return privacy-safe diagnostics for an Eqiva config entry."""
    coordinator = entry.runtime_data
    client = coordinator.client

    return {
        "entry": async_redact_data(entry.as_dict(), _TO_REDACT),
        "runtime": {
            "connection_mode": coordinator.connection_mode,
            "poll_interval_minutes": coordinator.poll_interval_minutes,
            "live_mode": coordinator.live_mode,
            "last_update_success": coordinator.last_update_success,
            "last_error": coordinator._last_error,
            "poll_sequence": coordinator._poll_sequence,
            "last_poll_succeeded": coordinator._last_poll_succeeded,
            "transport": _transport_diagnostics(client.transport),
            "status": _status_diagnostics(coordinator.data),
        },
    }
