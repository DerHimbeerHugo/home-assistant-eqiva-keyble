from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .ha_gatt_transport import HomeAssistantGattTransport
from .raw_att_transport import RawAttTransport
from .transport import EqivaTransport, TransportType, parse_transport_type

_LOGGER = logging.getLogger(__name__)


def create_transport(
    hass: HomeAssistant,
    address: str,
    name: str,
    requested: str,
) -> EqivaTransport:
    """Create exactly one transport; never fall back after a connect error."""
    selected = parse_transport_type(requested)
    _LOGGER.debug(
        "Eqiva %s: selected Bluetooth transport=%s (no fallback)",
        address,
        selected,
    )

    if selected is TransportType.RAW_ATT:
        return RawAttTransport(hass, address, name)
    return HomeAssistantGattTransport(hass, address, name)
