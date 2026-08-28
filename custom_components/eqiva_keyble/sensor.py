from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ADDRESS, CONF_NAME
from .coordinator import EqivaCoordinator
from .entity import EqivaEntity

BATTERY_OK = "ok"
BATTERY_LOW = "low"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EqivaCoordinator = entry.runtime_data
    async_add_entities([EqivaBatteryStatusSensor(coordinator, entry)])


class EqivaBatteryStatusSensor(EqivaEntity, SensorEntity):
    """Expose the only battery information reported by KeyBLE: low / not low."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = [BATTERY_OK, BATTERY_LOW]
    _attr_translation_key = "battery_status"

    def __init__(self, coordinator: EqivaCoordinator, entry: ConfigEntry) -> None:
        address = entry.data[CONF_ADDRESS]
        super().__init__(
            coordinator,
            entry.entry_id,
            address,
            entry.data.get(CONF_NAME, entry.title),
        )
        self._attr_unique_id = f"{address.replace(chr(58), '').lower()}_battery_status"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return BATTERY_LOW if self.coordinator.data.battery_low else BATTERY_OK

    @property
    def icon(self) -> str:
        if self.coordinator.data and self.coordinator.data.battery_low:
            return "mdi:battery-alert"
        return "mdi:battery"
