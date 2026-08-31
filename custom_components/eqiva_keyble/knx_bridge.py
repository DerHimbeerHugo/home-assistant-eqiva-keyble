from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    CONF_KNX_AVAILABLE_ADDRESS,
    CONF_KNX_BATTERY_LOW_ADDRESS,
    CONF_KNX_ENABLED,
    CONF_KNX_LOCK_ADDRESS,
    CONF_KNX_LOCKED_STATE_ADDRESS,
    CONF_KNX_OPEN_ADDRESS,
    CONF_KNX_UNLOCK_ADDRESS,
    DEFAULT_KNX_ENABLED,
    KNX_ADDRESS_OPTIONS,
)
from .coordinator import EqivaCoordinator

_LOGGER = logging.getLogger(__name__)

_KNX_DOMAIN = "knx"
_KNX_EVENT = "knx_event"
_KNX_EVENT_REGISTER = "event_register"
_KNX_SEND = "send"
_KNX_DPT_SWITCH = "1.001"


class EqivaKnxBridge:
    """Connect one Eqiva config entry to Home Assistant's KNX/IP session."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EqivaCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.enabled = bool(
            entry.options.get(CONF_KNX_ENABLED, DEFAULT_KNX_ENABLED)
        )
        self.addresses = {
            option: str(entry.options.get(option, "")).strip()
            for option in KNX_ADDRESS_OPTIONS
            if str(entry.options.get(option, "")).strip()
        }
        self._commands = {
            address: command
            for option, command in (
                (CONF_KNX_LOCK_ADDRESS, "lock"),
                (CONF_KNX_UNLOCK_ADDRESS, "unlock"),
                (CONF_KNX_OPEN_ADDRESS, "open"),
            )
            if (address := self.addresses.get(option))
        }
        self._status_options = {
            address: option
            for option in (
                CONF_KNX_LOCKED_STATE_ADDRESS,
                CONF_KNX_BATTERY_LOW_ADDRESS,
                CONF_KNX_AVAILABLE_ADDRESS,
            )
            if (address := self.addresses.get(option))
        }
        self._unsub_event = None
        self._unsub_coordinator = None
        self._command_task: asyncio.Task[None] | None = None
        self._publish_task: asyncio.Task[None] | None = None
        self._publish_pending = False
        self._last_sent: dict[str, bool] = {}
        self._started = False
        self._reconfigure_lock = asyncio.Lock()

    async def async_start(self) -> None:
        """Register configured group addresses and publish the initial state."""
        if not self.enabled:
            return

        if not self.hass.services.has_service(_KNX_DOMAIN, _KNX_EVENT_REGISTER):
            _LOGGER.warning(
                "Eqiva KNX bridge is enabled, but the Home Assistant KNX "
                "integration is not configured or not ready"
            )
            return

        registered = sorted(set(self.addresses.values()))
        if not registered:
            return

        try:
            await self.hass.services.async_call(
                _KNX_DOMAIN,
                _KNX_EVENT_REGISTER,
                {
                    "address": registered,
                    "type": _KNX_DPT_SWITCH,
                    "remove": False,
                },
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not register Eqiva KNX group addresses")
            return

        self._unsub_event = self.hass.bus.async_listen(
            _KNX_EVENT,
            self._handle_knx_event,
        )
        self._unsub_coordinator = self.coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        self._started = True
        _LOGGER.info(
            "Eqiva KNX bridge registered %d group address(es) with DPT %s: %s",
            len(registered),
            _KNX_DPT_SWITCH,
            ", ".join(registered),
        )
        await self._async_publish_all(force=True)

    async def async_reconfigure(self) -> None:
        """Apply changed group addresses without restarting the BLE client."""
        async with self._reconfigure_lock:
            await self.async_stop()
            self.enabled = bool(
                self.entry.options.get(CONF_KNX_ENABLED, DEFAULT_KNX_ENABLED)
            )
            self.addresses = {
                option: str(self.entry.options.get(option, "")).strip()
                for option in KNX_ADDRESS_OPTIONS
                if str(self.entry.options.get(option, "")).strip()
            }
            self._commands = {
                address: command
                for option, command in (
                    (CONF_KNX_LOCK_ADDRESS, "lock"),
                    (CONF_KNX_UNLOCK_ADDRESS, "unlock"),
                    (CONF_KNX_OPEN_ADDRESS, "open"),
                )
                if (address := self.addresses.get(option))
            }
            self._status_options = {
                address: option
                for option in (
                    CONF_KNX_LOCKED_STATE_ADDRESS,
                    CONF_KNX_BATTERY_LOW_ADDRESS,
                    CONF_KNX_AVAILABLE_ADDRESS,
                )
                if (address := self.addresses.get(option))
            }
            self._last_sent.clear()
            self._command_task = None
            self._publish_task = None
            self._publish_pending = False
            await self.async_start()

    @callback
    def _handle_knx_event(self, event: Event) -> None:
        """React only to incoming KNX telegrams for this lock."""
        data = event.data
        if data.get("direction") != "Incoming":
            return

        destination = str(data.get("destination", ""))
        telegram_type = data.get("telegramtype")
        if (
            destination not in self._commands
            and destination not in self._status_options
        ):
            return

        _LOGGER.debug(
            "Eqiva KNX telegram received: destination=%s, type=%s, "
            "value=%r, data=%r",
            destination,
            telegram_type,
            data.get("value"),
            data.get("data"),
        )

        if telegram_type == "GroupValueRead" and destination in self._status_options:
            self.hass.async_create_task(
                self._async_send_status(destination, response=True, force=True),
                f"Eqiva KNX read response {destination}",
            )
            return

        if (
            telegram_type != "GroupValueWrite"
            or destination not in self._commands
            or not _event_value_is_on(data)
        ):
            return

        if self._command_task is not None and not self._command_task.done():
            _LOGGER.warning(
                "Ignoring Eqiva KNX %s command while another lock command is active",
                self._commands[destination],
            )
            return

        command = self._commands[destination]
        self._command_task = self.hass.async_create_task(
            self._async_execute_command(command),
            f"Eqiva KNX {command}",
        )

    async def _async_execute_command(self, command: str) -> None:
        """Execute one KNX command without ever retrying the motor command."""
        _LOGGER.info("Executing Eqiva KNX command: %s", command)
        try:
            if command == "lock":
                await self.coordinator.async_lock()
            elif command == "unlock":
                await self.coordinator.async_unlock()
            else:
                await self.coordinator.async_open()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Eqiva KNX %s command failed", command)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coalesce coordinator updates into one KNX status publication."""
        self._publish_pending = True
        if self._publish_task is not None and not self._publish_task.done():
            return
        self._publish_task = self.hass.async_create_task(
            self._async_publish_loop(),
            "Eqiva KNX status publish",
        )

    async def _async_publish_loop(self) -> None:
        while self._publish_pending:
            self._publish_pending = False
            await self._async_publish_all()

    async def _async_publish_all(self, *, force: bool = False) -> None:
        for address in self._status_options:
            await self._async_send_status(address, force=force)

    async def _async_send_status(
        self,
        address: str,
        *,
        response: bool = False,
        force: bool = False,
    ) -> None:
        option = self._status_options.get(address)
        if option is None:
            return

        value = self._status_value(option)
        if not response and not force and self._last_sent.get(address) == value:
            return

        try:
            await self.hass.services.async_call(
                _KNX_DOMAIN,
                _KNX_SEND,
                {
                    "address": address,
                    "type": _KNX_DPT_SWITCH,
                    "payload": value,
                    "response": response,
                },
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not send Eqiva KNX status to %s", address)
            return

        if not response:
            self._last_sent[address] = value

    def _status_value(self, option: str) -> bool:
        if option == CONF_KNX_AVAILABLE_ADDRESS:
            return bool(self.coordinator.last_update_success)
        if self.coordinator.data is None:
            return False
        if option == CONF_KNX_BATTERY_LOW_ADDRESS:
            return bool(self.coordinator.data.battery_low)
        return bool(self.coordinator.data.is_locked)

    async def async_stop(self) -> None:
        """Detach listeners and unregister dynamically added KNX addresses."""
        if self._unsub_event is not None:
            self._unsub_event()
            self._unsub_event = None
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None

        command_task = self._command_task
        if command_task is not None and not command_task.done():
            # Do not cancel an ambiguous motor operation after it may have been sent.
            await command_task

        publish_task = self._publish_task
        if publish_task is not None and not publish_task.done():
            publish_task.cancel()
            try:
                await publish_task
            except asyncio.CancelledError:
                pass

        if self._started and self.hass.services.has_service(
            _KNX_DOMAIN, _KNX_EVENT_REGISTER
        ):
            try:
                await self.hass.services.async_call(
                    _KNX_DOMAIN,
                    _KNX_EVENT_REGISTER,
                    {
                        "address": sorted(set(self.addresses.values())),
                        "remove": True,
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Could not unregister Eqiva KNX group addresses")
        self._started = False


def _event_value_is_on(data: dict[str, Any]) -> bool:
    """Return true only for an explicit KNX DPT-1 on value."""
    value = data.get("value")
    if value is True or value == 1:
        return True
    raw = data.get("data")
    return raw is True or raw == 1 or raw == [1]
