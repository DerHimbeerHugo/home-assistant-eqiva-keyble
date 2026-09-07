from __future__ import annotations

from homeassistant.core import HomeAssistant

from .adaptive_transport import AdaptiveTransport
from .transport import EqivaTransport


def create_transport(
    hass: HomeAssistant,
    address: str,
    name: str,
) -> EqivaTransport:
    """Create the internal adaptive Eqiva Bluetooth transport.

    The concrete backend is selected immediately before every new BLE
    connection. Local hci paths use Raw ATT; stronger non-local Home Assistant
    paths such as ESPHome Bluetooth Proxies use HA GATT. The choice remains
    internal and is never exposed as a user setting.
    """
    return AdaptiveTransport(hass, address, name)
