from __future__ import annotations

from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.text import TextEntity, TextEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_ADDRESS,
    CONF_KNX_AVAILABLE_ADDRESS,
    CONF_KNX_BATTERY_LOW_ADDRESS,
    CONF_KNX_ENABLED,
    CONF_KNX_LOCK_ADDRESS,
    CONF_KNX_LOCKED_STATE_ADDRESS,
    CONF_KNX_OPEN_ADDRESS,
    CONF_KNX_UNLOCK_ADDRESS,
    CONF_NAME,
    DEFAULT_KNX_ENABLED,
    DOMAIN,
    KNX_ADDRESS_OPTIONS,
)
from .knx_address import normalize_knx_group_address
from .knx_bridge import EqivaKnxBridge


KNX_TEXT_DESCRIPTIONS = (
    TextEntityDescription(
        key="knx_lock_address",
        translation_key="knx_lock_address",
        icon="mdi:lock",
        entity_category=EntityCategory.CONFIG,
    ),
    TextEntityDescription(
        key="knx_unlock_address",
        translation_key="knx_unlock_address",
        icon="mdi:lock-open",
        entity_category=EntityCategory.CONFIG,
    ),
    TextEntityDescription(
        key="knx_open_address",
        translation_key="knx_open_address",
        icon="mdi:door-open",
        entity_category=EntityCategory.CONFIG,
    ),
    TextEntityDescription(
        key="knx_locked_state_address",
        translation_key="knx_locked_state_address",
        icon="mdi:lock-check",
        entity_category=EntityCategory.CONFIG,
    ),
    TextEntityDescription(
        key="knx_battery_low_address",
        translation_key="knx_battery_low_address",
        icon="mdi:battery-alert",
        entity_category=EntityCategory.CONFIG,
    ),
    TextEntityDescription(
        key="knx_available_address",
        translation_key="knx_available_address",
        icon="mdi:connection",
        entity_category=EntityCategory.CONFIG,
    ),
)

KNX_OPTION_BY_ENTITY_KEY = {
    "knx_lock_address": CONF_KNX_LOCK_ADDRESS,
    "knx_unlock_address": CONF_KNX_UNLOCK_ADDRESS,
    "knx_open_address": CONF_KNX_OPEN_ADDRESS,
    "knx_locked_state_address": CONF_KNX_LOCKED_STATE_ADDRESS,
    "knx_battery_low_address": CONF_KNX_BATTERY_LOW_ADDRESS,
    "knx_available_address": CONF_KNX_AVAILABLE_ADDRESS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    address = entry.data[CONF_ADDRESS]
    address_key = address.replace(":", "").lower()

    if not entry.options.get(CONF_KNX_ENABLED, DEFAULT_KNX_ENABLED):
        entity_registry = er.async_get(hass)
        for description in KNX_TEXT_DESCRIPTIONS:
            if entity_id := entity_registry.async_get_entity_id(
                TEXT_DOMAIN,
                DOMAIN,
                f"{address_key}_{description.key}",
            ):
                entity_registry.async_remove(entity_id)
        return

    async_add_entities(
        [
            EqivaKnxGroupAddressText(entry, description)
            for description in KNX_TEXT_DESCRIPTIONS
        ]
    )


class EqivaKnxGroupAddressText(TextEntity):
    """Allow a KNX group address to be edited on the device page."""

    _attr_has_entity_name = True
    _attr_native_min = 0
    _attr_native_max = 12
    _attr_pattern = r"^$|^\d+(?:/\d+){0,2}$"

    def __init__(
        self,
        entry: ConfigEntry,
        description: TextEntityDescription,
    ) -> None:
        self._entry = entry
        self.entity_description = description
        self._option = KNX_OPTION_BY_ENTITY_KEY[description.key]
        address = entry.data[CONF_ADDRESS]
        address_key = address.replace(":", "").lower()
        self._attr_unique_id = f"{address_key}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=entry.data.get(CONF_NAME, entry.title),
            manufacturer="eQ-3 AG",
            model="Eqiva Bluetooth Smart Lock (Key-BLE)",
        )

    @property
    def native_value(self) -> str:
        """Return the currently stored group address."""
        return str(self._entry.options.get(self._option, ""))

    async def async_set_value(self, value: str) -> None:
        """Validate, persist and activate one group address."""
        try:
            normalized = normalize_knx_group_address(value)
        except ValueError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_knx_group_address",
            ) from err

        addresses = {
            option: str(self._entry.options.get(option, "")).strip()
            for option in KNX_ADDRESS_OPTIONS
        }
        addresses[self._option] = normalized
        configured = [address for address in addresses.values() if address]
        if len(configured) != len(set(configured)):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="duplicate_knx_group_address",
            )

        options = dict(self._entry.options)
        options[self._option] = normalized
        self.hass.config_entries.async_update_entry(self._entry, options=options)

        bridge = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if isinstance(bridge, EqivaKnxBridge):
            await bridge.async_reconfigure()
        self.async_write_ha_state()
