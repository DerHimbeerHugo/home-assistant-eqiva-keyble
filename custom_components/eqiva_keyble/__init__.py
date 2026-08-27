from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

# Import the Eqiva-specific BlueZ notify compatibility patch before any client
# instances are created. This keeps config-flow and runtime clients on the same
# tested connection path while we validate the lock's timing behavior.
from . import bluez_notify_patch as _bluez_notify_patch  # noqa: F401
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
