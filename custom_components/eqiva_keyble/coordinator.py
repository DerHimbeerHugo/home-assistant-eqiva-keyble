from __future__ import annotations

from datetime import timedelta
import logging

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
            update_interval=timedelta(minutes=10),
        )
        self.client = client

    async def _async_update_data(self) -> EqivaStatus:
        try:
            return await self.client.status()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err

    async def async_lock(self) -> None:
        status = await self.client.lock()
        self.async_set_updated_data(status)

    async def async_unlock(self) -> None:
        status = await self.client.unlock()
        self.async_set_updated_data(status)

    async def async_open(self) -> None:
        status = await self.client.open()
        self.async_set_updated_data(status)
