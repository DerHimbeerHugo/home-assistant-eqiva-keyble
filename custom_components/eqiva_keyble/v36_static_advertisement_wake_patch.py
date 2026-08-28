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
_DIAGNOSTIC_MARKER = "SESSION-DIAG-v0.2"

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


def _local_path_summary(self: EqivaKeyBleClient) -> str:
    path = _LIVE_LOCAL_RAW_PATH(self)
    if path is None:
        return "none"

    scanner = path.scanner
    advertisement = path.advertisement
    rssi = advertisement.rssi
    return (
        "present("
        f"source={scanner.source},adapter={scanner.adapter},"
        f"adapter_idx={scanner.adapter_idx},"
        f"rssi={rssi if rssi is not None else 'unknown'}"
        ")"
    )


async def _wait_for_fresh_local_advertisement(
    self: EqivaKeyBleClient,
    *,
    timeout: float = 12.0,
) -> str:
    """Wait for the next genuinely new Eqiva advertisement from local hci."""
    loop = asyncio.get_running_loop()
    wait_started = loop.time()
    seen: asyncio.Future[tuple[str, int | None, str, str]] = loop.create_future()

    path_before_clear = _local_path_summary(self)
    cached_device_before_clear = self._fresh_ble_device() is not None
    _LOGGER.debug(
        "Eqiva %s: %s WAKE-START timeout=%.1fs cached_ble_device=%s "
        "local_path_before_clear=%s",
        self.address,
        _DIAGNOSTIC_MARKER,
        timeout,
        cached_device_before_clear,
        path_before_clear,
    )

    # Eqiva advertisements are effectively static. Home Assistant normally
    # suppresses unchanged repeats before integration callbacks. Clearing the
    # history makes the next radio packet count as new again; this API exists
    # specifically for static advertisements used as a wake signal before GATT.
    bluetooth.async_clear_advertisement_history(self.hass, self.address)
    _LOGGER.debug(
        "Eqiva %s: %s advertisement history cleared after %.3fs",
        self.address,
        _DIAGNOSTIC_MARKER,
        loop.time() - wait_started,
    )

    def _advertisement_received(service_info, change) -> None:
        if seen.done():
            return
        source = getattr(service_info, "source", None)
        if not _is_local_hci_source(self, source):
            return
        rssi = getattr(service_info, "rssi", None)
        change_name = getattr(change, "name", str(change))
        path_at_callback = _local_path_summary(self)
        _LOGGER.debug(
            "Eqiva %s: %s FRESH-ADVERTISEMENT after %.3fs source=%s "
            "rssi=%s change=%s local_path_at_callback=%s",
            self.address,
            _DIAGNOSTIC_MARKER,
            loop.time() - wait_started,
            source,
            rssi if rssi is not None else "unknown",
            change_name,
            path_at_callback,
        )
        seen.set_result((source, rssi, change_name, path_at_callback))

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
            source, rssi, change_name, path_at_callback = await seen
    except TimeoutError as err:
        _LOGGER.warning(
            "Eqiva %s: %s WAKE-TIMEOUT after %.3fs cached_ble_device=%s "
            "local_path_now=%s",
            self.address,
            _DIAGNOSTIC_MARKER,
            loop.time() - wait_started,
            self._fresh_ble_device() is not None,
            _local_path_summary(self),
        )
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
            _LOGGER.debug(
                "Eqiva %s: %s WAKE-READY after %.3fs source=%s rssi=%s "
                "change=%s callback_path=%s current_path=%s",
                self.address,
                _DIAGNOSTIC_MARKER,
                loop.time() - wait_started,
                source,
                rssi if rssi is not None else "unknown",
                change_name,
                path_at_callback,
                _local_path_summary(self),
            )
            return source
        await asyncio.sleep(0.025)

    raise EqivaNotFoundError(
        f"{_RAW_MARKER}: frisches lokales Eqiva-Advertisement über {source} empfangen, "
        "aber Home Assistant hat danach keinen aktuellen raw-ATT Scanner-Pfad veröffentlicht"
    )


async def _connect_v36(self: EqivaKeyBleClient) -> None:
    if self._client is not None and self._client.is_connected:
        _LOGGER.debug(
            "Eqiva %s: %s CONNECT-SKIP existing client is connected",
            self.address,
            _DIAGNOSTIC_MARKER,
        )
        return

    loop = asyncio.get_running_loop()
    started = loop.time()
    source = await _wait_for_fresh_local_advertisement(self)
    _LOGGER.debug(
        "Eqiva %s: %s CONNECT-START one raw ATT session after fresh "
        "advertisement source=%s local_path=%s",
        self.address,
        _DIAGNOSTIC_MARKER,
        source,
        _local_path_summary(self),
    )
    try:
        await _BASE_CONNECT(self)
    except (EqivaConnectionError, EqivaNotFoundError) as err:
        _LOGGER.debug(
            "Eqiva %s: %s CONNECT-FAILED after %.3fs error=%s: %s "
            "local_path_now=%s",
            self.address,
            _DIAGNOSTIC_MARKER,
            loop.time() - started,
            type(err).__name__,
            err,
            _local_path_summary(self),
        )
        raise

    _LOGGER.debug(
        "Eqiva %s: %s CONNECT-SUCCESS after %.3fs source=%s",
        self.address,
        _DIAGNOSTIC_MARKER,
        loop.time() - started,
        source,
    )


EqivaKeyBleClient._connect = _connect_v36
