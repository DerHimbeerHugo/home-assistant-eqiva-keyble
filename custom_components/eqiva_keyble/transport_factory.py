from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .ha_gatt_transport import HomeAssistantGattTransport
from .raw_att_transport import RawAttTransport, local_raw_path
from .transport import (
    EqivaTransport,
    TransportType,
    parse_transport_type,
    resolve_transport_type,
)

_LOGGER = logging.getLogger(__name__)


def create_transport(
    hass: HomeAssistant,
    address: str,
    name: str,
    requested: str,
) -> EqivaTransport:
    """Resolve one transport before connecting; never fall back after an error."""
    requested_type = parse_transport_type(requested)
    selected = resolve_transport_type(
        requested_type,
        local_raw_available=local_raw_path(hass, address) is not None,
    )

    if requested_type is TransportType.AUTO:
        _LOGGER.debug(
            "Eqiva %s: Bluetooth transport auto-resolved to %s (no fallback)",
            address,
            selected,
        )
    else:
        _LOGGER.debug(
            "Eqiva %s: selected Bluetooth transport=%s (no fallback)",
            address,
            selected,
        )

    if selected is TransportType.RAW_ATT:
        return RawAttTransport(hass, address, name)
    return HomeAssistantGattTransport(hass, address, name)
