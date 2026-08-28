from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

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

_LIVE_RECONNECT_DELAYS = (2, 5, 10, 20, 30, 60)


class EqivaCoordinator(DataUpdateCoordinator[EqivaStatus]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: EqivaKeyBleClient,
        poll_interval_minutes: int = DEFAULT_POLL_INTERVAL,
        connection_mode: str = DEFAULT_CONNECTION_MODE,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Eqiva Key-BLE",
            update_interval=timedelta(minutes=poll_interval_minutes),
        )
        self.client = client
        self.poll_interval_minutes = poll_interval_minutes
        self.connection_mode = connection_mode
        self.live_mode = connection_mode == CONNECTION_MODE_LIVE
        self._stopping = False
        self._reconnect_task: asyncio.Task[None] | None = None

        if isinstance(client, EqivaLiveKeyBleClient):
            client.set_live_callbacks(
                self._handle_live_status,
                self._handle_live_disconnect,
            )

    async def _async_update_data(self) -> EqivaStatus:
        try:
            return await self.client.status()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err

    def _handle_live_status(self, status: EqivaStatus) -> None:
        """Publish a status received immediately after STATUS_CHANGED."""
        if self._stopping:
            return
        self.async_set_updated_data(status)

    def _handle_live_disconnect(self) -> None:
        """Mark live data unavailable and start one reconnect loop."""
        if self._stopping or not self.live_mode:
            return

        self.async_set_update_error(
            UpdateFailed("Bluetooth-Dauerverbindung zum Eqiva Schloss getrennt")
        )
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
                self.async_set_updated_data(status)
                return
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def async_lock(self) -> None:
        status = await self.client.lock()
        self.async_set_updated_data(status)

    async def async_unlock(self) -> None:
        status = await self.client.unlock()
        self.async_set_updated_data(status)

    async def async_open(self) -> None:
        status = await self.client.open()
        self.async_set_updated_data(status)

    async def async_shutdown(self) -> None:
        """Stop reconnect work and close a persistent KeyBLE session."""
        self._stopping = True
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
