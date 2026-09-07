from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .exceptions import EqivaConnectionError
from .ha_gatt_transport import HomeAssistantGattTransport
from .raw_att_transport import RawAttTransport, is_local_raw_path
from .transport import (
    DisconnectCallback,
    EqivaTransport,
    NotificationCallback,
    TransportType,
)
from .transport_selection import (
    RSSI_SWITCH_MARGIN_DB,
    TransportChoice,
    choose_transport_kind,
)

_LOGGER = logging.getLogger(__name__)


def _path_rssi(path: Any) -> int:
    rssi = path.advertisement.rssi
    return rssi if rssi is not None else -127


def _strongest_path(paths: list[Any]) -> Any | None:
    if not paths:
        return None
    return max(paths, key=_path_rssi)


class AdaptiveTransport(EqivaTransport):
    """Select the best Eqiva Bluetooth backend for every new connection.

    Local Linux hci paths can use the Eqiva-specific Raw ATT compatibility
    backend. Non-local Home Assistant paths, such as ESPHome Bluetooth Proxies,
    use HA GATT. A small RSSI hysteresis avoids switching back and forth on
    reconnects when both paths have nearly identical reception.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
    ) -> None:
        super().__init__(address, name)
        self.hass = hass
        self._transport: EqivaTransport | None = None
        self._last_kind: TransportType | None = None
        self._selection_reason: str | None = None
        self._selected_source: str | None = None
        self._local_source: str | None = None
        self._local_rssi: int | None = None
        self._nonlocal_source: str | None = None
        self._nonlocal_rssi: int | None = None

    @property
    def kind(self) -> TransportType:
        transport = self._transport
        if transport is not None:
            return transport.kind
        return self._last_kind or TransportType.HA_GATT

    @property
    def is_connected(self) -> bool:
        transport = self._transport
        return bool(transport is not None and transport.is_connected)

    @property
    def active_transport(self) -> EqivaTransport | None:
        """Return the currently selected concrete transport for diagnostics."""
        return self._transport

    def _select_transport(self) -> tuple[EqivaTransport, TransportChoice]:
        paths = bluetooth.async_scanner_devices_by_address(
            self.hass,
            self.address,
            connectable=True,
        )
        local_paths = [path for path in paths if is_local_raw_path(path)]
        nonlocal_paths = [path for path in paths if not is_local_raw_path(path)]

        local_path = _strongest_path(local_paths)
        nonlocal_path = _strongest_path(nonlocal_paths)

        local_rssi = (
            local_path.advertisement.rssi if local_path is not None else None
        )
        nonlocal_rssi = (
            nonlocal_path.advertisement.rssi if nonlocal_path is not None else None
        )
        choice = choose_transport_kind(
            local_available=local_path is not None,
            local_rssi=local_rssi,
            nonlocal_available=nonlocal_path is not None,
            nonlocal_rssi=nonlocal_rssi,
            previous_kind=self._last_kind,
        )

        self._local_source = (
            str(local_path.scanner.source) if local_path is not None else None
        )
        self._local_rssi = local_rssi
        self._nonlocal_source = (
            str(nonlocal_path.scanner.source) if nonlocal_path is not None else None
        )
        self._nonlocal_rssi = nonlocal_rssi
        self._selection_reason = choice.reason
        self._last_kind = choice.kind

        if choice.kind == TransportType.RAW_ATT:
            self._selected_source = self._local_source
            transport: EqivaTransport = RawAttTransport(
                self.hass,
                self.address,
                self.name,
            )
        else:
            preferred_source = self._nonlocal_source
            self._selected_source = preferred_source
            transport = HomeAssistantGattTransport(
                self.hass,
                self.address,
                self.name,
                preferred_source=preferred_source,
            )

        _LOGGER.debug(
            "Eqiva %s: adaptive Bluetooth selection=%s source=%s "
            "local_rssi=%s nonlocal_rssi=%s margin=%s reason=%s",
            self.address,
            choice.kind,
            self._selected_source or "auto",
            local_rssi if local_rssi is not None else "unavailable",
            nonlocal_rssi if nonlocal_rssi is not None else "unavailable",
            RSSI_SWITCH_MARGIN_DB,
            choice.reason,
        )
        return transport, choice

    async def connect(
        self,
        notification_callback: NotificationCallback,
        disconnected_callback: DisconnectCallback,
    ) -> None:
        if self.is_connected:
            return

        transport, _choice = self._select_transport()
        self._transport = transport
        await transport.connect(notification_callback, disconnected_callback)

    async def write(self, data: bytes) -> None:
        transport = self._transport
        if transport is None or not transport.is_connected:
            raise EqivaConnectionError("Kein aktiver Eqiva-Bluetooth-Transport")
        await transport.write(data)

    async def session_ready(self) -> None:
        transport = self._transport
        if transport is None:
            raise EqivaConnectionError("Kein aktiver Eqiva-Bluetooth-Transport")
        await transport.session_ready()

    async def disconnect(self) -> None:
        transport = self._transport
        if transport is not None:
            await transport.disconnect()
