from __future__ import annotations

import asyncio
import logging

from habluetooth import BluetoothScanningMode
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.models import BluetoothCallbackReplay

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from . import v35_advertisement_connect_patch as _v35_patch
from .protocol import EqivaConnectionError, EqivaKeyBleClient, EqivaNotFoundError

_LOGGER = logging.getLogger(__name__)
_RAW_MARKER = "RAW-PDU-v36"

_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER

# Keep the proven raw ATT implementation. v36 only changes how a sleeping Eqiva
# is synchronized to a fresh local advertisement before opening the connection.
_BASE_CONNECT = _v35_patch._BASE_CONNECT
_LIVE_LOCAL_RAW_PATH = _v35_patch._LIVE_LOCAL_RAW_PATH


def _consume_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Eqiva background active scan failed", exc_info=True)


def _is_local_hci_source(self: EqivaKeyBleClient, source: str | None) -> bool:
    if not source:
        return False
    scanner = bluetooth.async_scanner_by_source(self.hass, source)
    if scanner is None:
        return False
    return bool(
        scanner.adapter_idx is not None
        and scanner.adapter.startswith("hci")
        and isinstance(scanner.source, str)
        and scanner.source.count(":") == 5
    )


async def _wait_for_fresh_local_advertisement(
    self: EqivaKeyBleClient,
    *,
    timeout: float = 12.0,
) -> str:
    """Wait for the next genuinely new Eqiva advertisement from local hci."""
    loop = asyncio.get_running_loop()
    seen: asyncio.Future[str] = loop.create_future()

    # Eqiva advertisements are effectively static. Home Assistant normally
    # suppresses unchanged repeats before integration callbacks. Clearing the
    # history makes the next radio packet count as new again; this API exists
    # specifically for static advertisements used as a wake signal before GATT.
    bluetooth.async_clear_advertisement_history(self.hass, self.address)

    def _advertisement_received(service_info, change) -> None:
        if seen.done():
            return
        source = getattr(service_info, "source", None)
        if not _is_local_hci_source(self, source):
            return
        seen.set_result(source)

    unload = bluetooth.async_register_callback(
        self.hass,
        _advertisement_received,
        {"address": self.address},
        BluetoothScanningMode.ACTIVE,
        replay=BluetoothCallbackReplay.DISABLED,
    )

    scan_task = self.hass.async_create_task(
        bluetooth.async_request_active_scan(self.hass, duration=10.0),
        f"eqiva-v36-scan-{self.address}",
    )
    scan_task.add_done_callback(_consume_task_result)

    try:
        async with asyncio.timeout(timeout):
            source = await seen
    except TimeoutError as err:
        raise EqivaNotFoundError(
            f"{_RAW_MARKER}: innerhalb von {timeout:.0f} Sekunden wurde kein neues "
            "Eqiva-Advertisement von einem lokalen hci-Adapter empfangen"
        ) from err
    finally:
        unload()

    # The callback and the scanner-device path are published by adjacent parts
    # of HA's Bluetooth manager. Give the path list a very short bounded window
    # to catch up instead of requiring both states in the callback itself.
    deadline = loop.time() + 0.75
    while loop.time() < deadline:
        if _LIVE_LOCAL_RAW_PATH(self) is not None:
            return source
        await asyncio.sleep(0.025)

    raise EqivaNotFoundError(
        f"{_RAW_MARKER}: frisches lokales Eqiva-Advertisement über {source} empfangen, "
        "aber Home Assistant hat danach keinen aktuellen raw-ATT Scanner-Pfad veröffentlicht"
    )


async def _connect_v36(self: EqivaKeyBleClient) -> None:
    if self._client is not None and self._client.is_connected:
        return

    last_error: Exception | None = None

    for wake_attempt in range(1, 4):
        try:
            source = await _wait_for_fresh_local_advertisement(self)
        except EqivaNotFoundError as err:
            last_error = err
            if wake_attempt < 3:
                _LOGGER.warning(
                    "Eqiva %s: %s wake attempt %d/3 saw no usable fresh local advertisement; retrying",
                    self.address,
                    _RAW_MARKER,
                    wake_attempt,
                )
                continue
            raise

        _LOGGER.debug(
            "Eqiva %s: %s fresh advertisement from local source %s; raw ATT wake attempt %d/3",
            self.address,
            _RAW_MARKER,
            source,
            wake_attempt,
        )

        try:
            await _BASE_CONNECT(self)
            return
        except EqivaNotFoundError as err:
            # The short-lived scanner path can disappear between advertisement
            # callback and raw connect. Wait for the next radio packet rather
            # than using stale scanner objects.
            last_error = err
            continue
        except EqivaConnectionError as err:
            last_error = err
            text = str(err)
            if "Errno 38" not in text and "Function not implemented" not in text:
                raise
            _LOGGER.warning(
                "Eqiva %s: %s raw L2CAP establishment returned ENOSYS after fresh advertisement "
                "(attempt %d/3); waiting for the next advertising cycle",
                self.address,
                _RAW_MARKER,
                wake_attempt,
            )
            continue

    raise EqivaConnectionError(
        f"{_RAW_MARKER}: raw L2CAP konnte nach drei frischen lokalen Eqiva-Werbezyklen "
        f"nicht aufgebaut werden. Letzter Fehler: {last_error}"
    )


EqivaKeyBleClient._connect = _connect_v36
