from __future__ import annotations

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ADDRESS, CONF_NAME, DOMAIN
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
    address = entry.data[CONF_ADDRESS]
    address_key = address.replace(":", "").lower()

    # v0.1.x exposed battery_low and pairing_allowed as binary sensors. Remove
    # those registry entries during the v0.2.0 migration so users do not keep
    # unavailable/orphaned diagnostic entities after the platform change.
    entity_registry = er.async_get(hass)
    for legacy_unique_id in (
        f"{address_key}_battery_low",
        f"{address_key}_pairing_allowed",
    ):
        if entity_id := entity_registry.async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            legacy_unique_id,
        ):
            entity_registry.async_remove(entity_id)

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
