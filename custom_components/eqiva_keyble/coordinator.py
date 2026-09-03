from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from time import monotonic

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONNECTION_MODE_LIVE,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_POLL_INTERVAL,
)
from .live_client import EqivaLiveKeyBleClient
from .protocol import EqivaKeyBleClient, EqivaStatus

_LOGGER = logging.getLogger(__name__)

_DIAGNOSTIC_MARKER = "SESSION-DIAG"
_LIVE_RECONNECT_DELAYS = (2, 5, 10, 20, 30, 60)
_LIVE_KEEPALIVE_INTERVAL = 3 * 60


class EqivaCoordinator(DataUpdateCoordinator[EqivaStatus]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: EqivaKeyBleClient,
        poll_interval_minutes: int = DEFAULT_POLL_INTERVAL,
        connection_mode: str = DEFAULT_CONNECTION_MODE,
    ) -> None:
        live_mode = connection_mode == CONNECTION_MODE_LIVE
        super().__init__(
            hass,
            _LOGGER,
            name="Eqiva Key-BLE",
            update_interval=(
                None
                if live_mode
                else timedelta(minutes=poll_interval_minutes)
            ),
        )
        self.client = client
        self.poll_interval_minutes = poll_interval_minutes
        self.connection_mode = connection_mode
        self.live_mode = live_mode
        self._stopping = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._live_activity_event = asyncio.Event()
        self._last_live_activity: float | None = None
        self._poll_sequence = 0
        self._last_poll_started: float | None = None
        self._last_poll_finished: float | None = None
        self._last_poll_succeeded: bool | None = None
        self._last_error: str | None = None

        if isinstance(client, EqivaLiveKeyBleClient):
            client.set_live_callbacks(
                self._handle_live_status,
                self._handle_live_disconnect,
            )

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
        client_connected = self.client.is_connected

        _LOGGER.debug(
            "Eqiva %s: %s poll=%d START "
            "since_previous_start=%s idle_since_previous_finish=%s "
            "previous_result=%s client_connected=%s mode=%s interval=%s",
            self.client.address,
            _DIAGNOSTIC_MARKER,
            sequence,
            _format_seconds(since_start),
            _format_seconds(idle_since_finish),
            _format_result(self._last_poll_succeeded),
            client_connected,
            self.connection_mode,
            (
                "keepalive-only"
                if self.live_mode
                else f"{self.poll_interval_minutes}min"
            ),
        )
        self._last_poll_started = started

        try:
            status = await self.client.status()
        except Exception as err:  # noqa: BLE001
            finished = monotonic()
            self._last_poll_finished = finished
            self._last_poll_succeeded = False
            self._last_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning(
                "Eqiva %s: %s poll=%d FAILED after %.3fs error=%s: %s",
                self.client.address,
                _DIAGNOSTIC_MARKER,
                sequence,
                finished - started,
                type(err).__name__,
                err,
            )
            raise UpdateFailed(str(err)) from err

        finished = monotonic()
        self._last_poll_finished = finished
        self._last_poll_succeeded = True
        self._last_error = None
        self._record_live_activity(finished)
        _LOGGER.debug(
            "Eqiva %s: %s poll=%d SUCCESS after %.3fs "
            "lock_status=%d battery_low=%s pairing_allowed=%s",
            self.client.address,
            _DIAGNOSTIC_MARKER,
            sequence,
            finished - started,
            status.lock_status,
            status.battery_low,
            status.pairing_allowed,
        )
        return status

    def _handle_live_status(self, status: EqivaStatus) -> None:
        """Publish a status received immediately after STATUS_CHANGED."""
        if self._stopping:
            return
        self._last_error = None
        self._record_live_activity()
        self.async_set_updated_data(status)

    def async_start_live_keepalive(self) -> None:
        """Start the independent live-session keepalive after initial setup."""
        if (
            not self.live_mode
            or self._stopping
            or (
                self._keepalive_task is not None
                and not self._keepalive_task.done()
            )
        ):
            return

        if self._last_live_activity is None:
            self._record_live_activity()
        self._keepalive_task = self.hass.async_create_task(
            self._async_live_keepalive_loop(),
            "Eqiva live keepalive",
        )

    def _record_live_activity(self, timestamp: float | None = None) -> None:
        """Move the keepalive deadline after successful live-session traffic."""
        if not self.live_mode:
            return
        self._last_live_activity = timestamp if timestamp is not None else monotonic()
        self._live_activity_event.set()

    async def _async_live_keepalive_loop(self) -> None:
        """Keep an idle live session open with a status request every three minutes."""
        try:
            while not self._stopping and self.live_mode:
                self._live_activity_event.clear()
                last_activity = self._last_live_activity or monotonic()
                remaining = max(
                    0.0,
                    _LIVE_KEEPALIVE_INTERVAL - (monotonic() - last_activity),
                )
                try:
                    await asyncio.wait_for(
                        self._live_activity_event.wait(),
                        timeout=remaining,
                    )
                    continue
                except TimeoutError:
                    pass

                if self._stopping:
                    return
                if (
                    self._last_live_activity is not None
                    and monotonic() - self._last_live_activity
                    < _LIVE_KEEPALIVE_INTERVAL
                ):
                    continue
                if (
                    self._reconnect_task is not None
                    and not self._reconnect_task.done()
                ):
                    await self._live_activity_event.wait()
                    continue

                started = monotonic()
                try:
                    status = await self.client.status()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    self._last_error = f"{type(err).__name__}: {err}"
                    _LOGGER.debug(
                        "Eqiva %s: live keepalive failed: %s",
                        self.client.address,
                        err,
                        exc_info=True,
                    )
                    # A client-side abort is intentionally hidden from the
                    # transport's disconnect callback. Explicitly hand the
                    # failed keepalive to the coordinator reconnect loop too.
                    self._handle_live_disconnect()
                    continue

                self._last_error = None
                self._record_live_activity()
                _LOGGER.debug(
                    "Eqiva %s: live keepalive succeeded after %.3fs",
                    self.client.address,
                    monotonic() - started,
                )
                # Avoid unnecessary listener updates when the keepalive merely
                # confirms the already published state.
                if status != self.data:
                    self.async_set_updated_data(status)
        finally:
            if self._keepalive_task is asyncio.current_task():
                self._keepalive_task = None

    def _handle_live_disconnect(self) -> None:
        """Mark live data unavailable and start one reconnect loop."""
        if self._stopping or not self.live_mode:
            return

        message = "Bluetooth-Dauerverbindung zum Eqiva Schloss getrennt"
        if self._last_error is None:
            self._last_error = message
        self.async_set_update_error(UpdateFailed(message))
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return

        self._reconnect_task = self.hass.async_create_task(
            self._async_live_reconnect_loop(),
            "Eqiva live reconnect",
        )

    async def _async_live_reconnect_loop(self) -> None:
        """Reconnect a dropped live session with bounded backoff."""
        attempt = 0
        try:
            while not self._stopping and self.live_mode:
                delay = _LIVE_RECONNECT_DELAYS[
                    min(attempt, len(_LIVE_RECONNECT_DELAYS) - 1)
                ]
                await asyncio.sleep(delay)
                if self._stopping:
                    return
                try:
                    status = await self.client.status()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    attempt += 1
                    self._last_error = f"{type(err).__name__}: {err}"
                    _LOGGER.debug(
                        "Eqiva live reconnect attempt %s failed: %s",
                        attempt,
                        err,
                        exc_info=True,
                    )
                    continue

                _LOGGER.debug(
                    "Eqiva live connection restored after %s reconnect attempt(s)",
                    attempt + 1,
                )
                self._last_error = None
                self._record_live_activity()
                self.async_set_updated_data(status)
                return
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def async_lock(self) -> None:
        status = await self.client.lock()
        self._last_error = None
        self._record_live_activity()
        self.async_set_updated_data(status)

    async def async_unlock(self) -> None:
        status = await self.client.unlock()
        self._last_error = None
        self._record_live_activity()
        self.async_set_updated_data(status)

    async def async_open(self) -> None:
        status = await self.client.open()
        self._last_error = None
        self._record_live_activity()
        self.async_set_updated_data(status)

    async def async_shutdown(self) -> None:
        """Stop reconnect work and close a persistent KeyBLE session."""
        self._stopping = True
        self._live_activity_event.set()

        keepalive_task = self._keepalive_task
        self._keepalive_task = None
        if keepalive_task is not None and not keepalive_task.done():
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if isinstance(self.client, EqivaLiveKeyBleClient):
            await self.client.async_shutdown()

        await super().async_shutdown()


def _format_seconds(value: float | None) -> str:
    return "first" if value is None else f"{value:.3f}s"


def _format_result(value: bool | None) -> str:
    if value is None:
        return "none"
    return "success" if value else "failed"
