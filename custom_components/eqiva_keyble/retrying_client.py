from __future__ import annotations

import asyncio
import logging

from .protocol import (
    MSG_COMMAND,
    STATUS_LOCKED,
    STATUS_OPENED,
    STATUS_UNLOCKED,
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
    EqivaStatus,
)

_LOGGER = logging.getLogger(__name__)

_RETRYABLE_CONNECTION_ERRORS = (
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaNotFoundError,
)
_CONNECTION_RETRY_DELAY = 1.0


class EqivaRetryingKeyBleClient(EqivaKeyBleClient):
    """KeyBLE client with one immediate retry before an operation starts."""

    async def _ensure_session_with_retry(self) -> None:
        """Establish BLE + KeyBLE session, retrying one transient failure.

        The retry happens before any motor command is sent. This is intentional:
        lock/unlock/open commands must never be blindly duplicated after an
        ambiguous write or response timeout.
        """
        for attempt in range(1, 3):
            try:
                await self._connect()
                await self._ensure_nonces_exchanged()
                return
            except _RETRYABLE_CONNECTION_ERRORS as err:
                await self._abort_connection()
                if attempt == 2:
                    raise
                _LOGGER.debug(
                    "Eqiva %s: connection/session attempt %s failed (%s); "
                    "retrying once after %.1f s",
                    self.address,
                    attempt,
                    err,
                    _CONNECTION_RETRY_DELAY,
                )
                await asyncio.sleep(_CONNECTION_RETRY_DELAY)

    async def status(self) -> EqivaStatus:
        """Read status with one immediate reconnect retry."""
        async with self._operation_lock:
            try:
                await self._ensure_session_with_retry()
                return await self.request_status()
            finally:
                await self._disconnect()

    async def _command(
        self,
        command: int,
        targets: set[int],
    ) -> EqivaStatus:
        """Prepare the session with retry, then send the motor command once."""
        async with self._operation_lock:
            try:
                await self._ensure_session_with_retry()

                # From this point onward the command is deliberately never retried:
                # a transport timeout can be ambiguous and the lock may already have
                # accepted the first COMMAND frame.
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
            finally:
                await self._disconnect()

    async def lock(self) -> EqivaStatus:
        return await self._command(0, {STATUS_LOCKED})

    async def unlock(self) -> EqivaStatus:
        return await self._command(1, {STATUS_UNLOCKED})

    async def open(self) -> EqivaStatus:
        return await self._command(
            2,
            {STATUS_OPENED, STATUS_UNLOCKED},
        )
