from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum


class TransportType(StrEnum):
    """Internal Bluetooth backend used by the Eqiva integration."""

    RAW_ATT = "raw_att"
    HA_GATT = "ha_gatt"


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
