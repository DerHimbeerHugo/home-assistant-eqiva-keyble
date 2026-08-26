from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ADDRESS, CONF_NAME
from .coordinator import EqivaCoordinator
from .entity import EqivaEntity
from .protocol import STATUS_MOVING, STATUS_OPENED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EqivaCoordinator = entry.runtime_data
    async_add_entities([EqivaLock(coordinator, entry)])


class EqivaLock(EqivaEntity, LockEntity):
    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: EqivaCoordinator, entry: ConfigEntry) -> None:
        address = entry.data[CONF_ADDRESS]
        name = entry.data.get(CONF_NAME, entry.title)
        super().__init__(coordinator, entry.entry_id, address, name)
        self._attr_unique_id = f"{address.replace(chr(58), '').lower()}_lock"

    @property
    def is_locked(self) -> bool | None:
        return self.coordinator.data.is_locked if self.coordinator.data else None

    @property
    def is_locking(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.lock_status == STATUS_MOVING)

    @property
    def is_unlocking(self) -> bool:
        return False

    @property
    def is_open(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.lock_status == STATUS_OPENED)

    async def async_lock(self, **kwargs: Any) -> None:
        await self.coordinator.async_lock()

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.async_unlock()

    async def async_open(self, **kwargs: Any) -> None:
        await self.coordinator.async_open()
