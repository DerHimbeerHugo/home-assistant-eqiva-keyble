from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .ha_gatt_transport import HomeAssistantGattTransport
from .raw_att_transport import RawAttTransport, local_raw_path
from .transport import EqivaTransport

_LOGGER = logging.getLogger(__name__)


def create_transport(
    hass: HomeAssistant,
    address: str,
    name: str,
) -> EqivaTransport:
    """Select the required Eqiva Bluetooth backend without exposing a user option.

    Local Linux/BlueZ paths use the proven Eqiva raw-ATT compatibility backend.
    Other Home Assistant Bluetooth paths, including ESPHome proxies, use the
    normal Home Assistant/Bleak GATT backend. Selection happens before a KeyBLE
    session starts; motor commands are never replayed on another backend.
    """
    path = local_raw_path(hass, address)
    if path is not None:
        scanner = path.scanner
        _LOGGER.debug(
            "Eqiva %s: internal Bluetooth backend=raw_att source=%s adapter=%s rssi=%s",
            address,
            scanner.source,
            scanner.adapter,
            path.advertisement.rssi,
        )
        return RawAttTransport(hass, address, name)

    _LOGGER.debug(
        "Eqiva %s: internal Bluetooth backend=ha_gatt; no current local hci path",
        address,
    )
    return HomeAssistantGattTransport(hass, address, name)
