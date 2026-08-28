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
# v29 established the proven transport baseline: notifications arrive without
# an active CCCD write, so the failed SMP/CCCD experiments remain disabled.
from . import v29_diagnostic_patch as _eqiva_v29_diagnostic_patch  # noqa: F401
# v32 keeps the successful fresh Key-Card pairing path.
from . import v32_pairing_probe as _eqiva_v32_pairing_probe  # noqa: F401
# v33/v34/v35 remain imported only as historical layering for the captured base
# raw-connect functions; each newer patch overrides the previous connect hook.
from . import v33_path_resilience_patch as _eqiva_v33_path_resilience_patch  # noqa: F401
from . import v34_fresh_path_patch as _eqiva_v34_fresh_path_patch  # noqa: F401
from . import v35_advertisement_connect_patch as _eqiva_v35_advertisement_connect_patch  # noqa: F401
# v36 clears HA's static advertisement history and synchronizes raw L2CAP to the
# next genuinely new local hci advertisement from the sleeping Eqiva lock.
from . import v36_static_advertisement_wake_patch as _eqiva_v36_static_advertisement_wake_patch  # noqa: F401
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
