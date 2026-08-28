from __future__ import annotations

import asyncio
import logging

from habluetooth import BluetoothScanningMode
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.models import BluetoothCallbackReplay

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from . import v34_fresh_path_patch as _v34_patch
from .protocol import EqivaConnectionError, EqivaKeyBleClient, EqivaNotFoundError

_LOGGER = logging.getLogger(__name__)
_RAW_MARKER = "RAW-PDU-v35"

_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER

# Use the proven raw ATT connection implementation directly. v35 does not cache
# scanner objects and does not use v34's ENOSYS rescan wrapper. Instead every
# connection attempt is synchronized to a newly received local advertisement.
_BASE_CONNECT = _v34_patch._BASE_CONNECT
_LIVE_LOCAL_RAW_PATH = _v34_patch._LIVE_LOCAL_RAW_PATH


def _consume_task_result(task: asyncio.Task) -> None:
    """Consume an on-demand scan task result so background errors are logged."""
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Eqiva background active scan failed", exc_info=True)


async def _wait_for_fresh_local_advertisement(
    self: EqivaKeyBleClient,
    *,
    timeout: float = 9.0,
) -> None:
    """Wait for a new advertisement that is visible through a local hci scanner."""
    loop = asyncio.get_running_loop()
    seen: asyncio.Future[None] = loop.create_future()

    def _advertisement_received(service_info, change) -> None:
        if seen.done():
            return
        # The same Eqiva advertisement may also arrive through a Bluetooth
        # proxy. Only continue when Home Assistant simultaneously exposes a
        # connectable local hci path for the lock.
        if _LIVE_LOCAL_RAW_PATH(self) is not None:
            seen.set_result(None)

    unload = bluetooth.async_register_callback(
        self.hass,
        _advertisement_received,
        {"address": self.address},
        BluetoothScanningMode.ACTIVE,
        replay=BluetoothCallbackReplay.DISABLED,
    )

    # Start a bus-wide active sweep, but do not wait for the full sweep window:
    # as soon as this specific lock advertises we want to initiate the connection
    # while that wake/advertising cycle is fresh. The scanner's connecting()
    # context will coordinate discovery with the subsequent raw connection.
    scan_task = self.hass.async_create_task(
        bluetooth.async_request_active_scan(self.hass, duration=8.0),
        f"eqiva-v35-scan-{self.address}",
    )
    scan_task.add_done_callback(_consume_task_result)

    try:
        async with asyncio.timeout(timeout):
            await seen
    except TimeoutError as err:
        raise EqivaNotFoundError(
            f"{_RAW_MARKER}: innerhalb von {timeout:.0f} Sekunden wurde kein frisches "
            "Eqiva-Advertisement über einen lokalen hci-Adapter empfangen"
        ) from err
    finally:
        unload()

    # Let Home Assistant finish publishing the scanner-device path created by
    # the callback before the raw backend reads it.
    await asyncio.sleep(0)


async def _connect_v35(self: EqivaKeyBleClient) -> None:
    if self._client is not None and self._client.is_connected:
        return

    last_error: Exception | None = None

    # ENOSYS from a Bluetooth L2CAP connect does not necessarily mean the system
    # call is missing. Linux bt_to_errno() maps otherwise-unhandled HCI status
    # values to ENOSYS; in practice a failed LE establishment (for example HCI
    # 0x3e) can surface exactly this way. Retry only after the lock advertises
    # again rather than immediately hammering the sleeping peripheral.
    for wake_attempt in range(1, 4):
        await _wait_for_fresh_local_advertisement(self)
        _LOGGER.debug(
            "Eqiva %s: %s fresh local advertisement received; raw ATT wake attempt %d/3",
            self.address,
            _RAW_MARKER,
            wake_attempt,
        )

        try:
            await _BASE_CONNECT(self)
            return
        except EqivaConnectionError as err:
            last_error = err
            text = str(err)
            if "Errno 38" not in text and "Function not implemented" not in text:
                raise
            _LOGGER.warning(
                "Eqiva %s: %s raw L2CAP establishment returned ENOSYS after wake attempt %d/3; "
                "waiting for the next fresh advertisement",
                self.address,
                _RAW_MARKER,
                wake_attempt,
            )
            continue

    raise EqivaConnectionError(
        f"{_RAW_MARKER}: raw L2CAP konnte nach drei frischen Eqiva-Advertisements "
        f"nicht aufgebaut werden. Letzter Fehler: {last_error}"
    )


EqivaKeyBleClient._connect = _connect_v35
