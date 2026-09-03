from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_CONNECTION_MODE,
    CONF_KEY_CARD,
    CONF_KNX_ENABLED,
    CONF_NAME,
    CONF_POLL_INTERVAL,
    CONF_USER_ID,
    CONF_USER_KEY,
    CONNECTION_MODE_LIVE,
    CONNECTION_MODE_POLLING,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_KNX_ENABLED,
    DEFAULT_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    KNX_ADDRESS_OPTIONS,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .knx_address import normalize_knx_group_address
from .protocol import (
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
    canonical_address,
    parse_key_card,
)
from .transport_factory import create_transport

_LOGGER = logging.getLogger(__name__)

EQIVA_MANUFACTURER_ID = 0x1A00


class EqivaNoScannerError(EqivaNotFoundError):
    """Home Assistant has no connectable Bluetooth scanner."""


class EqivaAddressMismatchError(EqivaNotFoundError):
    """An Eqiva advertisement was found under another Bluetooth address."""


def _connection_mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[CONNECTION_MODE_POLLING, CONNECTION_MODE_LIVE],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="connection_mode",
        )
    )


def _eqiva_discoveries(hass) -> list:
    """Return connectable Eqiva advertisements known to Home Assistant."""
    return [
        info
        for info in bluetooth.async_discovered_service_info(hass, connectable=True)
        if EQIVA_MANUFACTURER_ID in info.manufacturer_data
    ]


def _current_connection_paths(hass, address: str) -> list:
    """Return current connectable scanner paths for the lock."""
    return bluetooth.async_scanner_devices_by_address(
        hass, address, connectable=True
    )


async def _async_ensure_lock_seen(hass, address: str) -> None:
    """Ensure Home Assistant has a current connection path to the lock."""
    scanner_count = bluetooth.async_scanner_count(hass, connectable=True)
    _LOGGER.debug(
        "Eqiva discovery: %s connectable Home Assistant Bluetooth scanner(s)",
        scanner_count,
    )
    if scanner_count == 0:
        raise EqivaNoScannerError(
            "Home Assistant hat keinen connectable Bluetooth-Adapter oder Bluetooth-Proxy"
        )

    current_paths = _current_connection_paths(hass, address)
    if current_paths:
        _LOGGER.debug(
            "Eqiva %s: %s current connectable scanner path(s) available",
            address,
            len(current_paths),
        )
        return

    in_history = (
        bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        is not None
    )
    _LOGGER.debug(
        "Eqiva %s: no current scanner path (in connectable history=%s); "
        "requesting 10 second active scan",
        address,
        in_history,
    )
    await bluetooth.async_request_active_scan(hass, duration=10.0)

    current_paths = _current_connection_paths(hass, address)
    if current_paths:
        _LOGGER.debug(
            "Eqiva %s: fresh connection path discovered during active scan via %s scanner(s)",
            address,
            len(current_paths),
        )
        return

    discoveries = _eqiva_discoveries(hass)
    if discoveries:
        discovered_addresses = sorted({info.address.upper() for info in discoveries})
        if address.upper() in discovered_addresses:
            raise EqivaNotFoundError(
                f"{address} wurde zwar im Home-Assistant-Bluetooth-Verlauf gesehen, "
                "aber auch nach 10 Sekunden aktiver Suche steht kein aktueller "
                "connectable Scanner-Pfad zur Verfügung"
            )
        _LOGGER.warning(
            "Eqiva advertisement(s) with manufacturer ID 0x1A00 found at %s, "
            "but requested address was %s",
            discovered_addresses,
            address,
        )
        raise EqivaAddressMismatchError(
            "Eqiva-Advertisement 0x1A00 gefunden, aber nicht unter der erwarteten Adresse"
        )

    raise EqivaNotFoundError(
        f"{address} wurde auch nach 10 Sekunden aktiver Bluetooth-Suche nicht gefunden; "
        "kein Eqiva-Advertisement mit Manufacturer-ID 0x1A00 empfangen"
    )


class EqivaKeyBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EqivaKeyBleOptionsFlow:
        """Create the Eqiva options flow."""
        return EqivaKeyBleOptionsFlow()

    async def async_step_bluetooth(self, discovery_info) -> ConfigFlowResult:
        """Handle native Home Assistant Bluetooth discovery."""
        if EQIVA_MANUFACTURER_ID not in discovery_info.manufacturer_data:
            return self.async_abort(reason="not_supported")

        address = canonical_address(discovery_info.address)
        await self.async_set_unique_id(address.replace(":", "").lower())
        self._abort_if_unique_id_configured()
        self._discovered_address = address
        self.context["title_placeholders"] = {
            "name": discovery_info.name or address,
        }
        _LOGGER.debug(
            "Eqiva %s discovered by manufacturer ID 0x1A00 via Home Assistant Bluetooth",
            address,
        )
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start every new setup directly with the original Eqiva Key Card."""
        return await self.async_step_key_card()

    async def async_step_key_card(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        description_placeholders = {"error": "–"}
        if user_input is not None:
            try:
                card = parse_key_card(user_input[CONF_KEY_CARD])
                await self.async_set_unique_id(card.address.replace(":", "").lower())
                self._abort_if_unique_id_configured()
                await _async_ensure_lock_seen(self.hass, card.address)
                transport = create_transport(
                    self.hass,
                    card.address,
                    user_input[CONF_NAME],
                )
                client = EqivaKeyBleClient(
                    self.hass,
                    card.address,
                    name=user_input[CONF_NAME],
                    transport=transport,
                )
                user_id, user_key = await client.pair(card.key)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_ADDRESS: card.address,
                        CONF_USER_ID: user_id,
                        CONF_USER_KEY: user_key.hex(),
                    },
                    options={
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                        CONF_CONNECTION_MODE: user_input[CONF_CONNECTION_MODE],
                        CONF_KNX_ENABLED: bool(user_input[CONF_KNX_ENABLED]),
                    },
                )
            except AbortFlow:
                raise
            except ValueError as err:
                errors["base"] = "invalid_key_card"
                description_placeholders["error"] = str(err)
            except EqivaNoScannerError as err:
                _LOGGER.exception(
                    "No connectable Home Assistant Bluetooth scanner for Eqiva"
                )
                errors["base"] = "no_scanner"
                description_placeholders["error"] = str(err)
            except EqivaAddressMismatchError as err:
                _LOGGER.exception("Eqiva advertisement found under a different address")
                errors["base"] = "address_mismatch"
                description_placeholders["error"] = str(err)
            except EqivaNotFoundError as err:
                _LOGGER.exception("Eqiva lock was not found during pairing")
                errors["base"] = "not_found"
                description_placeholders["error"] = str(err)
            except EqivaConnectionError as err:
                _LOGGER.exception("Eqiva Bluetooth/GATT connection failed during pairing")
                errors["base"] = "connection_failed"
                description_placeholders["error"] = str(err)
            except EqivaHandshakeError as err:
                _LOGGER.exception("Eqiva nonce handshake failed during pairing")
                errors["base"] = "handshake_failed"
                description_placeholders["error"] = str(err)
            except EqivaProtocolError as err:
                _LOGGER.exception("Eqiva protocol pairing failed")
                errors["base"] = "pairing_failed"
                description_placeholders["error"] = str(err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected Eqiva pairing failure")
                errors["base"] = "pairing_failed"
                description_placeholders["error"] = f"{type(err).__name__}: {err}"

        return self.async_show_form(
            step_id="key_card",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_KEY_CARD): str,
                    vol.Required(
                        CONF_CONNECTION_MODE,
                        default=DEFAULT_CONNECTION_MODE,
                    ): _connection_mode_selector(),
                    vol.Required(
                        CONF_KNX_ENABLED,
                        default=DEFAULT_KNX_ENABLED,
                    ): bool,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
        )


class EqivaKeyBleOptionsFlow(OptionsFlowWithReload):
    """Manage Eqiva runtime options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure connection mode and the optional KNX bridge."""
        if user_input is not None:
            poll_interval = int(
                user_input.get(
                    CONF_POLL_INTERVAL,
                    self.config_entry.options.get(
                        CONF_POLL_INTERVAL,
                        DEFAULT_POLL_INTERVAL,
                    ),
                )
            )
            existing_addresses = {
                option: normalize_knx_group_address(
                    self.config_entry.options.get(option, "")
                )
                for option in KNX_ADDRESS_OPTIONS
            }
            return self.async_create_entry(
                title="",
                data={
                    CONF_POLL_INTERVAL: poll_interval,
                    CONF_CONNECTION_MODE: user_input[CONF_CONNECTION_MODE],
                    CONF_KNX_ENABLED: bool(user_input[CONF_KNX_ENABLED]),
                    **existing_addresses,
                },
            )

        displayed_options = self.config_entry.options
        current_interval = int(
            displayed_options.get(
                CONF_POLL_INTERVAL,
                DEFAULT_POLL_INTERVAL,
            )
        )
        current_mode = str(
            displayed_options.get(
                CONF_CONNECTION_MODE,
                DEFAULT_CONNECTION_MODE,
            )
        )
        current_knx_enabled = bool(
            displayed_options.get(CONF_KNX_ENABLED, DEFAULT_KNX_ENABLED)
        )
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_CONNECTION_MODE,
                default=current_mode,
            ): _connection_mode_selector(),
        }
        if current_mode != CONNECTION_MODE_LIVE:
            schema[
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current_interval,
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_POLL_INTERVAL,
                    max=MAX_POLL_INTERVAL,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        schema[
            vol.Required(
                CONF_KNX_ENABLED,
                default=current_knx_enabled,
            )
        ] = bool
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
        )
