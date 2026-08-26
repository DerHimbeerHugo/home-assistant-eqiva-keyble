from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EqivaCoordinator


class EqivaEntity(CoordinatorEntity[EqivaCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EqivaCoordinator, entry_id: str, address: str, name: str) -> None:
        super().__init__(coordinator)
        self._address = address
        self._entry_id = entry_id
        self._device_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=name,
            manufacturer="eQ-3 AG",
            model="Eqiva Bluetooth Smart Lock (Key-BLE)",
        )
