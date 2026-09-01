from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_ADDRESS,
    CONF_CONNECTION_MODE,
    CONF_NAME,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT,
    CONF_USER_ID,
    CONF_USER_KEY,
    CONNECTION_MODE_LIVE,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TRANSPORT,
    DOMAIN,
)
from .coordinator import EqivaCoordinator
from .knx_bridge import EqivaKnxBridge
from .live_client import EqivaLiveKeyBleClient
from .protocol import canonical_key
from .retrying_client import EqivaRetryingKeyBleClient
from .transport_factory import create_transport

PLATFORMS = [Platform.LOCK, Platform.SENSOR, Platform.TEXT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    connection_mode = str(
        entry.options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    )
    client_class = (
        EqivaLiveKeyBleClient
        if connection_mode == CONNECTION_MODE_LIVE
        else EqivaRetryingKeyBleClient
    )
    requested_transport = str(entry.options.get(CONF_TRANSPORT, DEFAULT_TRANSPORT))
    transport = create_transport(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data.get(CONF_NAME, entry.title),
        requested_transport,
    )
    client = client_class(
        hass,
        entry.data[CONF_ADDRESS],
        user_id=int(entry.data[CONF_USER_ID]),
        user_key=canonical_key(entry.data[CONF_USER_KEY]),
        name=entry.data.get(CONF_NAME, entry.title),
        transport=transport,
        requested_transport=requested_transport,
    )
    poll_interval = int(
        entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    )
    coordinator = EqivaCoordinator(
        hass,
        client,
        poll_interval_minutes=poll_interval,
        connection_mode=connection_mode,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    knx_bridge = EqivaKnxBridge(hass, entry, coordinator)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = knx_bridge
    await knx_bridge.async_start()
    coordinator.async_start_live_keepalive()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    if bridge := hass.data.get(DOMAIN, {}).pop(entry.entry_id, None):
        await bridge.async_stop()
    coordinator: EqivaCoordinator = entry.runtime_data
    await coordinator.async_shutdown()
    return True
