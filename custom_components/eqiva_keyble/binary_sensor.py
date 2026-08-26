from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_ADDRESS, CONF_NAME
from .coordinator import EqivaCoordinator
from .entity import EqivaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EqivaCoordinator = entry.runtime_data
    async_add_entities([
        EqivaBatteryLowSensor(coordinator, entry),
        EqivaPairingAllowedSensor(coordinator, entry),
    ])


class EqivaBatteryLowSensor(EqivaEntity, BinarySensorEntity):
    _attr_name = "Batterie schwach"
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EqivaCoordinator, entry: ConfigEntry) -> None:
        address = entry.data[CONF_ADDRESS]
        super().__init__(coordinator, entry.entry_id, address, entry.data.get(CONF_NAME, entry.title))
        self._attr_unique_id = f"{address.replace(chr(58), '').lower()}_battery_low"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.battery_low if self.coordinator.data else None


class EqivaPairingAllowedSensor(EqivaEntity, BinarySensorEntity):
    _attr_name = "Pairing erlaubt"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EqivaCoordinator, entry: ConfigEntry) -> None:
        address = entry.data[CONF_ADDRESS]
        super().__init__(coordinator, entry.entry_id, address, entry.data.get(CONF_NAME, entry.title))
        self._attr_unique_id = f"{address.replace(chr(58), '').lower()}_pairing_allowed"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.pairing_allowed if self.coordinator.data else None
