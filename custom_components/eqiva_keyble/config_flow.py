from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.data_entry_flow import AbortFlow

from .const import (
    CONF_ADDRESS,
    CONF_KEY_CARD,
    CONF_NAME,
    CONF_SETUP_METHOD,
    CONF_USER_ID,
    CONF_USER_KEY,
    DEFAULT_NAME,
    DOMAIN,
    SETUP_CREDENTIALS,
    SETUP_KEY_CARD,
)
from .protocol import (
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
    canonical_address,
    canonical_key,
    parse_key_card,
)

_LOGGER = logging.getLogger(__name__)

EQIVA_MANUFACTURER_ID = 0x1A00


class EqivaNoScannerError(EqivaNotFoundError):
    """Home Assistant has no connectable Bluetooth scanner."""


class EqivaAddressMismatchError(EqivaNotFoundError):
    """An Eqiva advertisement was found under another Bluetooth address."""


def _eqiva_discoveries(hass) -> list:
    """Return connectable Eqiva advertisements known to Home Assistant."""
    return [
        info
        for info in bluetooth.async_discovered_service_info(hass, connectable=True)
        if EQIVA_MANUFACTURER_ID in info.manufacturer_data
    ]


async def _async_ensure_lock_seen(hass, address: str) -> None:
    """Wait for Home Assistant to actually see the lock before connecting."""
    scanner_count = bluetooth.async_scanner_count(hass, connectable=True)
    _LOGGER.debug(
        "Eqiva discovery: %s connectable Home Assistant Bluetooth scanner(s)",
        scanner_count,
    )
    if scanner_count == 0:
        raise EqivaNoScannerError(
            "Home Assistant hat keinen connectable Bluetooth-Adapter oder Bluetooth-Proxy"
        )

    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is not None:
        _LOGGER.debug("Eqiva %s: already present in Home Assistant Bluetooth cache", address)
        return

    _LOGGER.debug(
        "Eqiva %s: not present in Bluetooth cache; requesting 10 second active scan",
        address,
    )
    await bluetooth.async_request_active_scan(hass, duration=10.0)

    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is not None:
        _LOGGER.debug("Eqiva %s: discovered during active Bluetooth scan", address)
        return

    discoveries = _eqiva_discoveries(hass)
    if discoveries:
        discovered_addresses = sorted({info.address.upper() for info in discoveries})
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_SETUP_METHOD] == SETUP_KEY_CARD:
                return await self.async_step_key_card()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_SETUP_METHOD, default=SETUP_KEY_CARD): vol.In({
                    SETUP_KEY_CARD: "Mit Eqiva Key Card koppeln",
                    SETUP_CREDENTIALS: "Vorhandene KeyBLE-Zugangsdaten verwenden",
                })
            }),
        )

    async def async_step_key_card(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                card = parse_key_card(user_input[CONF_KEY_CARD])
                await self.async_set_unique_id(card.address.replace(":", "").lower())
                self._abort_if_unique_id_configured()
                await _async_ensure_lock_seen(self.hass, card.address)
                client = EqivaKeyBleClient(self.hass, card.address, name=user_input[CONF_NAME])
                user_id, user_key = await client.pair(card.key)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_ADDRESS: card.address,
                        CONF_USER_ID: user_id,
                        CONF_USER_KEY: user_key.hex(),
                    },
                )
            except AbortFlow:
                raise
            except ValueError:
                errors["base"] = "invalid_key_card"
            except EqivaNoScannerError:
                _LOGGER.exception("No connectable Home Assistant Bluetooth scanner for Eqiva")
                errors["base"] = "no_scanner"
            except EqivaAddressMismatchError:
                _LOGGER.exception("Eqiva advertisement found under a different address")
                errors["base"] = "address_mismatch"
            except EqivaNotFoundError:
                _LOGGER.exception("Eqiva lock was not found during pairing")
                errors["base"] = "not_found"
            except EqivaConnectionError:
                _LOGGER.exception("Eqiva Bluetooth/GATT connection failed during pairing")
                errors["base"] = "connection_failed"
            except EqivaHandshakeError:
                _LOGGER.exception("Eqiva nonce handshake failed during pairing")
                errors["base"] = "handshake_failed"
            except EqivaProtocolError:
                _LOGGER.exception("Eqiva protocol pairing failed")
                errors["base"] = "pairing_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected Eqiva pairing failure")
                errors["base"] = "pairing_failed"

        return self.async_show_form(
            step_id="key_card",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_KEY_CARD): str,
            }),
            errors=errors,
        )

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                address = canonical_address(user_input[CONF_ADDRESS])
                key = canonical_key(user_input[CONF_USER_KEY])
                user_id = int(user_input[CONF_USER_ID])
                if not 0 <= user_id <= 255:
                    raise ValueError
                await self.async_set_unique_id(address.replace(":", "").lower())
                self._abort_if_unique_id_configured()
                await _async_ensure_lock_seen(self.hass, address)
                client = EqivaKeyBleClient(
                    self.hass, address, user_id=user_id, user_key=key, name=user_input[CONF_NAME]
                )
                await client.status()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_ADDRESS: address,
                        CONF_USER_ID: user_id,
                        CONF_USER_KEY: key.hex(),
                    },
                )
            except AbortFlow:
                raise
            except ValueError:
                errors["base"] = "invalid_credentials"
            except EqivaNoScannerError:
                _LOGGER.exception("No connectable Home Assistant Bluetooth scanner for Eqiva")
                errors["base"] = "no_scanner"
            except EqivaAddressMismatchError:
                _LOGGER.exception("Eqiva advertisement found under a different address")
                errors["base"] = "address_mismatch"
            except EqivaNotFoundError:
                _LOGGER.exception("Eqiva lock was not found during credential validation")
                errors["base"] = "not_found"
            except EqivaConnectionError:
                _LOGGER.exception("Eqiva Bluetooth/GATT connection failed")
                errors["base"] = "connection_failed"
            except EqivaHandshakeError:
                _LOGGER.exception("Eqiva nonce handshake failed")
                errors["base"] = "handshake_failed"
            except EqivaProtocolError:
                _LOGGER.exception("Eqiva authentication/protocol validation failed")
                errors["base"] = "authentication_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected Eqiva credential validation failure")
                errors["base"] = "cannot_connect"

        address_field = (
            vol.Required(CONF_ADDRESS, default=self._discovered_address)
            if self._discovered_address
            else vol.Required(CONF_ADDRESS)
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                address_field: str,
                vol.Required(CONF_USER_ID): vol.Coerce(int),
                vol.Required(CONF_USER_KEY): str,
            }),
            errors=errors,
        )