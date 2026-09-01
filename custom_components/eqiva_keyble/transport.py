from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum

from .const import TRANSPORT_HA_GATT, TRANSPORT_RAW_ATT


class TransportType(StrEnum):
    """Selectable Eqiva Bluetooth transports."""

    RAW_ATT = TRANSPORT_RAW_ATT
    HA_GATT = TRANSPORT_HA_GATT


NotificationCallback = Callable[[bytes], None]
DisconnectCallback = Callable[[], None]


class EqivaTransport(ABC):
    """Minimal Bluetooth transport required by the KeyBLE protocol."""

    kind: TransportType

    def __init__(
        self,
        address: str,
        name: str,
    ) -> None:
        self.address = address
        self.name = name

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return whether the underlying BLE link is connected."""

    @abstractmethod
    async def connect(
        self,
        notification_callback: NotificationCallback,
        disconnected_callback: DisconnectCallback,
    ) -> None:
        """Connect, discover services, and enable KeyBLE notifications."""

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write one KeyBLE ATT-sized fragment."""

    async def session_ready(self) -> None:
        """Run optional transport work after KeyBLE nonce exchange."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the underlying BLE link."""


def parse_transport_type(requested: str) -> TransportType:
    """Validate an explicit transport choice without fallback or auto-selection."""
    try:
        return TransportType(requested)
    except ValueError as err:
        raise ValueError(f"Unbekannter Eqiva-Transport: {requested}") from err
