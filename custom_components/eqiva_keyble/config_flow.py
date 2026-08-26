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


async def _async_ensure_lock_seen(hass, address: str) -> None:
    """Wait for Home Assistant to actually see the lock before connecting."""
    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is not None:
        _LOGGER.debug("Eqiva %s: already present in Home Assistant Bluetooth cache", address)
        return

    _LOGGER.debug(
        "Eqiva %s: not present in Bluetooth cache; requesting 10 second active scan",
        address,
    )
    await bluetooth.async_request_active_scan(hass, duration=10.0)

    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is None:
        raise EqivaNotFoundError(
            f"{address} wurde auch nach 10 Sekunden aktiver Bluetooth-Suche nicht gefunden"
        )

    _LOGGER.debug("Eqiva %s: discovered during active Bluetooth scan", address)


class EqivaKeyBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_ADDRESS): str,
                vol.Required(CONF_USER_ID): vol.Coerce(int),
                vol.Required(CONF_USER_KEY): str,
            }),
            errors=errors,
        )