from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient

from .protocol import (
    MSG_COMMAND,
    MSG_STATUS_CHANGED,
    STATUS_LOCKED,
    STATUS_OPENED,
    STATUS_UNLOCKED,
    EqivaKeyBleClient,
    EqivaProtocolError,
    EqivaStatus,
)

_LOGGER = logging.getLogger(__name__)


class EqivaLiveKeyBleClient(EqivaKeyBleClient):
    """KeyBLE client that keeps the BLE/session connection open."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._live_status_callback: Callable[[EqivaStatus], None] | None = None
        self._live_disconnect_callback: Callable[[], None] | None = None
        self._status_changed_task: asyncio.Task[None] | None = None
        self._closing = False

    def set_live_callbacks(
        self,
        status_callback: Callable[[EqivaStatus], None],
        disconnect_callback: Callable[[], None],
    ) -> None:
        """Register coordinator callbacks for push status and reconnect handling."""
        self._live_status_callback = status_callback
        self._live_disconnect_callback = disconnect_callback

    def _on_disconnect(self, client: BleakClient) -> None:
        """Reset the protocol session and request a reconnect while live mode is active."""
        super()._on_disconnect(client)
        if not self._closing and self._live_disconnect_callback is not None:
            self._live_disconnect_callback()

    def _handle_fragment(self, fragment: bytes) -> None:
        """Process KeyBLE data and react immediately to STATUS_CHANGED."""
        is_status_changed = (
            len(fragment) >= 2
            and bool(fragment[0] & 0x80)
            and (fragment[0] & 0x7F) == 0
            and fragment[1] == MSG_STATUS_CHANGED
        )
        super()._handle_fragment(fragment)

        if (
            is_status_changed
            and not self._closing
            and (
                self._status_changed_task is None
                or self._status_changed_task.done()
            )
        ):
            self._status_changed_task = self.hass.async_create_task(
                self._async_refresh_after_status_changed(),
                "Eqiva live STATUS_CHANGED refresh",
            )

    async def _async_refresh_after_status_changed(self) -> None:
        """Mirror original KeyBLE: STATUS_CHANGED immediately triggers STATUS_REQUEST."""
        try:
            async with self._operation_lock:
                if self._closing:
                    return
                status = await self.request_status()
            if self._live_status_callback is not None:
                self._live_status_callback(status)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Eqiva %s: live STATUS_CHANGED refresh failed: %s",
                self.address,
                err,
                exc_info=True,
            )
            await self._abort_connection()

    async def status(self) -> EqivaStatus:
        """Read status while preserving the established KeyBLE session."""
        async with self._operation_lock:
            try:
                await self._connect()
                return await self.request_status()
            except Exception:
                await self._abort_connection()
                raise

    async def _command(
        self,
        command: int,
        targets: set[int],
    ) -> EqivaStatus:
        """Execute a command without disconnecting after completion."""
        async with self._operation_lock:
            try:
                await self._connect()
                await self._send_message(
                    MSG_COMMAND,
                    bytes([command]),
                    secure=True,
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 20.0
                last: EqivaStatus | None = None
                while loop.time() < deadline:
                    await asyncio.sleep(0.75)
                    last = await self.request_status()
                    if last.lock_status in targets:
                        return last
                if last is not None:
                    return last
                raise EqivaProtocolError(
                    "Zeitüberschreitung beim Warten auf den Schlosszustand"
                )
            except Exception:
                await self._abort_connection()
                raise

    async def lock(self) -> EqivaStatus:
        return await self._command(0, {STATUS_LOCKED})

    async def unlock(self) -> EqivaStatus:
        return await self._command(1, {STATUS_UNLOCKED})

    async def open(self) -> EqivaStatus:
        return await self._command(
            2,
            {STATUS_OPENED, STATUS_UNLOCKED},
        )

    async def async_shutdown(self) -> None:
        """Stop push handling and close the live KeyBLE connection cleanly."""
        self._closing = True
        task = self._status_changed_task
        self._status_changed_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._disconnect()
