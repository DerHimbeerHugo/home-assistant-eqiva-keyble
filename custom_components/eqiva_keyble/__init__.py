from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

# Import the Eqiva-specific transport patch before any client instances are
# created. On local Linux adapters this uses habluetooth's raw L2CAP/ATT
# building blocks and deliberately skips the MTU exchange that breaks Key-BLE.
from . import bluez_notify_patch as _eqiva_transport_patch  # noqa: F401
# Add reproducible secure-frame diagnostics without changing the working raw
# ATT transport path. This records only session nonces/frame bytes, never keys.
from . import secure_trace_patch as _eqiva_secure_trace_patch  # noqa: F401
# v29 leaves the failed SMP experiments disabled. It tests notify delivery first
# without a CCCD write and, on the existing retry, with the proven Write Command,
# while exposing the discovered receive-characteristic/descriptor layout.
from . import v29_diagnostic_patch as _eqiva_v29_diagnostic_patch  # noqa: F401
from .const import CONF_ADDRESS, CONF_NAME, CONF_USER_ID, CONF_USER_KEY
from .coordinator import EqivaCoordinator
from .protocol import EqivaKeyBleClient, canonical_key

PLATFORMS = [Platform.LOCK, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = EqivaKeyBleClient(
        hass,
        entry.data[CONF_ADDRESS],
        user_id=int(entry.data[CONF_USER_ID]),
        user_key=canonical_key(entry.data[CONF_USER_KEY]),
        name=entry.data.get(CONF_NAME, entry.title),
    )
    coordinator = EqivaCoordinator(hass, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
