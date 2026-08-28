from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .protocol import EqivaKeyBleClient, EqivaStatus

_LOGGER = logging.getLogger(__name__)


class EqivaCoordinator(DataUpdateCoordinator[EqivaStatus]):
    def __init__(self, hass: HomeAssistant, client: EqivaKeyBleClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Eqiva Key-BLE",
            # Diagnostic build: reproduce the previously observed reconnect
            # failures at 10 minutes with the v40 idle diagnostics enabled.
            update_interval=timedelta(minutes=10),
        )
        self.client = client
        self._poll_sequence = 0
        self._last_poll_started: float | None = None
        self._last_poll_finished: float | None = None
        self._last_poll_succeeded: bool | None = None

    async def _async_update_data(self) -> EqivaStatus:
        started = monotonic()
        self._poll_sequence += 1
        sequence = self._poll_sequence
        since_start = (
            started - self._last_poll_started
            if self._last_poll_started is not None
            else None
        )
        idle_since_finish = (
            started - self._last_poll_finished
            if self._last_poll_finished is not None
            else None
        )
        client_connected = bool(
            self.client._client is not None and self.client._client.is_connected
        )

        _LOGGER.debug(
            "Eqiva %s: IDLE-DIAG-v40 poll=%d START "
            "since_previous_start=%s idle_since_previous_finish=%s "
            "previous_result=%s client_connected=%s",
            self.client.address,
            sequence,
            _format_seconds(since_start),
            _format_seconds(idle_since_finish),
            _format_result(self._last_poll_succeeded),
            client_connected,
        )
        self._last_poll_started = started

        try:
            status = await self.client.status()
        except Exception as err:  # noqa: BLE001
            finished = monotonic()
            self._last_poll_finished = finished
            self._last_poll_succeeded = False
            _LOGGER.warning(
                "Eqiva %s: IDLE-DIAG-v40 poll=%d FAILED after %.3fs "
                "error=%s: %s",
                self.client.address,
                sequence,
                finished - started,
                type(err).__name__,
                err,
            )
            raise UpdateFailed(str(err)) from err

        finished = monotonic()
        self._last_poll_finished = finished
        self._last_poll_succeeded = True
        _LOGGER.debug(
            "Eqiva %s: IDLE-DIAG-v40 poll=%d SUCCESS after %.3fs "
            "lock_status=%d battery_low=%s pairing_allowed=%s",
            self.client.address,
            sequence,
            finished - started,
            status.lock_status,
            status.battery_low,
            status.pairing_allowed,
        )
        return status

    async def async_lock(self) -> None:
        status = await self.client.lock()
        self.async_set_updated_data(status)

    async def async_unlock(self) -> None:
        status = await self.client.unlock()
        self.async_set_updated_data(status)

    async def async_open(self) -> None:
        status = await self.client.open()
        self.async_set_updated_data(status)


def _format_seconds(value: float | None) -> str:
    return "first" if value is None else f"{value:.3f}s"


def _format_result(value: bool | None) -> str:
    if value is None:
        return "none"
    return "success" if value else "failed"
