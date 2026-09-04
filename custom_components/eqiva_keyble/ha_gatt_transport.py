from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)
from habluetooth import BluetoothScanningMode
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.models import BluetoothCallbackReplay
from homeassistant.core import HomeAssistant

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID
from .exceptions import EqivaConnectionError, EqivaNotFoundError
from .transport import (
    DisconnectCallback,
    EqivaTransport,
    NotificationCallback,
    TransportType,
)

_LOGGER = logging.getLogger(__name__)

_ADVERTISEMENT_TIMEOUT = 12.0


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.debug("Eqiva HA-GATT active scan failed", exc_info=True)


class HomeAssistantGattTransport(EqivaTransport):
    """GATT transport through Home Assistant's Bluetooth stack.

    Home Assistant supplies the connectable BLEDevice. That device may belong
    to a local adapter or an ESPHome Bluetooth proxy; this transport never
    filters the path to hciX.
    """

    kind = TransportType.HA_GATT

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
    ) -> None:
        super().__init__(address, name)
        self.hass = hass
        self._client: BleakClient | None = None
        self._send_characteristic: Any | None = None
        self._receive_characteristic: Any | None = None
        self._disconnected_callback: DisconnectCallback | None = None
        self._suppress_disconnect_callback = False
        self._backend_name: str | None = None
        self._device_source: str | None = None
        self._rssi: int | None = None
        self._notify_mode: str | None = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    def _fresh_ble_device(self):
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    def _capture_device_details(self, device: Any) -> None:
        details = getattr(device, "details", None)
        if isinstance(details, dict):
            source = details.get("source")
            self._device_source = str(source) if source is not None else None
        else:
            source = getattr(details, "source", None)
            self._device_source = (
                str(source)
                if source is not None
                else type(details).__name__
                if details
                else None
            )

        paths = bluetooth.async_scanner_devices_by_address(
            self.hass, self.address, connectable=True
        )
        if paths:
            strongest = max(
                paths,
                key=lambda path: (
                    path.advertisement.rssi
                    if path.advertisement.rssi is not None
                    else -127
                ),
            )
            self._rssi = strongest.advertisement.rssi
            if self._device_source is None:
                self._device_source = str(strongest.scanner.source)

    async def _wait_for_fresh_advertisement(self) -> None:
        """Wait for a new advertisement from any connectable HA path."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        seen: asyncio.Future[tuple[str | None, int | None]] = loop.create_future()

        bluetooth.async_clear_advertisement_history(self.hass, self.address)

        def _advertisement_received(service_info, _change) -> None:
            if seen.done():
                return
            seen.set_result(
                (
                    getattr(service_info, "source", None),
                    getattr(service_info, "rssi", None),
                )
            )

        unload = bluetooth.async_register_callback(
            self.hass,
            _advertisement_received,
            {"address": self.address, "connectable": True},
            BluetoothScanningMode.ACTIVE,
            replay=BluetoothCallbackReplay.DISABLED,
        )
        scan_task = self.hass.async_create_task(
            bluetooth.async_request_active_scan(self.hass, duration=10.0),
            f"eqiva-ha-gatt-scan-{self.address}",
        )
        scan_task.add_done_callback(_consume_task_result)

        try:
            async with asyncio.timeout(_ADVERTISEMENT_TIMEOUT):
                source, rssi = await seen
        except TimeoutError as err:
            raise EqivaNotFoundError(
                "Innerhalb von 12 Sekunden wurde kein neues connectable "
                "Eqiva-Advertisement über Home Assistant empfangen"
            ) from err
        finally:
            unload()

        self._device_source = source
        self._rssi = rssi
        deadline = loop.time() + 0.75
        while loop.time() < deadline:
            if self._fresh_ble_device() is not None:
                break
            await asyncio.sleep(0.025)
        else:
            raise EqivaNotFoundError(
                "Frisches connectable Eqiva-Advertisement empfangen, aber "
                "Home Assistant hat danach kein verbindbares BLEDevice "
                "bereitgestellt"
            )
        _LOGGER.debug(
            "Eqiva %s: transport=%s fresh advertisement after %.3fs source=%s rssi=%s",
            self.address,
            self.kind,
            loop.time() - started,
            source or "unknown",
            rssi if rssi is not None else "unknown",
        )

    async def _clear_stale_connection(self) -> None:
        try:
            await close_stale_connections_by_address(self.address)
        except Exception:
            _LOGGER.debug(
                "Eqiva %s: transport=%s stale connection cleanup failed",
                self.address,
                self.kind,
                exc_info=True,
            )

    def _handle_disconnect(self, disconnected_client: BleakClient) -> None:
        if self._client is not disconnected_client:
            _LOGGER.debug(
                "Eqiva %s: transport=%s ignoring disconnect callback from "
                "stale or intentionally released client",
                self.address,
                self.kind,
            )
            return
        self._client = None
        self._send_characteristic = None
        self._receive_characteristic = None
        if (
            not self._suppress_disconnect_callback
            and self._disconnected_callback is not None
        ):
            self._disconnected_callback()

    async def _start_notifications(
        self,
        backend: Any,
        notification_callback: NotificationCallback,
        attempt: int,
    ) -> None:
        """Register Eqiva notifications without forcing proxy CCCD security.

        bleak-esphome connection-v3 first registers a proxy-side notification
        callback and then writes the CCCD itself. The Eqiva lock rejects that
        protected descriptor write with ATT 0x05 before the KeyBLE nonce
        exchange. For an ESPHome proxy, register the already available
        proxy-side notification callback and deliberately skip that descriptor
        write.
        """
        characteristic = self._receive_characteristic
        if characteristic is None:
            raise EqivaConnectionError(
                "Eqiva Receive-Characteristic ist nicht verfügbar"
            )

        backend_module = type(backend).__module__ if backend is not None else ""
        if backend_module.startswith("bleak_esphome."):
            api_client = getattr(backend, "_client", None)
            address_as_int = getattr(backend, "_address_as_int", None)
            notify_cancels = getattr(backend, "_notify_cancels", None)
            start_proxy_notify = getattr(
                api_client,
                "bluetooth_gatt_start_notify",
                None,
            )
            if (
                api_client is None
                or address_as_int is None
                or not isinstance(notify_cancels, dict)
                or start_proxy_notify is None
            ):
                raise EqivaConnectionError(
                    "Das ESPHome-Bleak-Backend stellt den für Eqiva benötigten "
                    "Local-Notify-Pfad ohne CCCD-Write nicht bereit"
                )

            ble_handle = characteristic.handle
            if ble_handle in notify_cancels:
                raise EqivaConnectionError(
                    "ESPHome-Notifications sind für die Eqiva-Characteristic "
                    "bereits registriert"
                )

            def _proxy_notification(_handle: int, data: bytearray) -> None:
                notification_callback(bytes(data))

            notify_cancels[ble_handle] = await start_proxy_notify(
                address_as_int,
                ble_handle,
                _proxy_notification,
            )
            self._notify_mode = "ESPHomeLocalOnly"
            _LOGGER.debug(
                "Eqiva %s: transport=%s registered ESPHome proxy notify handler "
                "without protected CCCD write; handle=%s",
                self.address,
                self.kind,
                ble_handle,
            )
            return

        is_local_bluez = "bluezdbus" in (self._backend_name or "").lower()
        use_acquire_notify = is_local_bluez and attempt == 1
        self._notify_mode = "AcquireNotify" if use_acquire_notify else "StartNotify"

        def _notification(_characteristic, data: bytearray) -> None:
            notification_callback(bytes(data))

        notify_kwargs = (
            {"bluez": {"use_start_notify": False}} if use_acquire_notify else {}
        )
        if self._client is None:
            raise EqivaConnectionError("HA-GATT-Client ist nicht verfügbar")
        await self._client.start_notify(
            characteristic,
            _notification,
            **notify_kwargs,
        )

    async def connect(
        self,
        notification_callback: NotificationCallback,
        disconnected_callback: DisconnectCallback,
    ) -> None:
        if self.is_connected:
            return

        self._disconnected_callback = disconnected_callback
        last_error: Exception | None = None

        for attempt in range(1, 3):
            if attempt > 1:
                await asyncio.sleep(1.5)
            await self._wait_for_fresh_advertisement()
            await self._clear_stale_connection()

            device = self._fresh_ble_device()
            if device is None:
                raise EqivaNotFoundError(
                    f"{self.address} wurde von Home Assistant Bluetooth nicht "
                    "als connectable Gerät bereitgestellt"
                )
            self._capture_device_details(device)

            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=1,
                    ble_device_callback=self._fresh_ble_device,
                    use_services_cache=attempt == 1,
                )

                backend = getattr(self._client, "_backend", None)
                self._backend_name = (
                    f"{type(backend).__module__}.{type(backend).__name__}"
                    if backend is not None
                    else "unknown"
                )

                services = self._client.services
                self._send_characteristic = services.get_characteristic(
                    SEND_CHARACTERISTIC_UUID
                )
                self._receive_characteristic = services.get_characteristic(
                    RECEIVE_CHARACTERISTIC_UUID
                )
                if self._send_characteristic is None:
                    raise EqivaConnectionError(
                        "Eqiva Send-Characteristic wurde im HA-GATT-Profil "
                        "nicht gefunden"
                    )
                if self._receive_characteristic is None:
                    raise EqivaConnectionError(
                        "Eqiva Receive-Characteristic wurde im HA-GATT-Profil "
                        "nicht gefunden"
                    )

                send_properties = set(self._send_characteristic.properties)
                receive_properties = set(self._receive_characteristic.properties)
                if "write" not in send_properties:
                    raise EqivaConnectionError(
                        "Der Eqiva-GATT-Pfad benötigt die Write-Request-Eigenschaft "
                        "'write'. 'write-without-response' ist ein anderer ATT-"
                        f"Vorgang. Gemeldet: {sorted(send_properties)}"
                    )
                if not ({"notify", "indicate"} & receive_properties):
                    raise EqivaConnectionError(
                        "Eqiva Receive-Characteristic unterstützt keine "
                        f"Notifications: {sorted(receive_properties)}"
                    )

                await asyncio.sleep(0.25)
                await self._start_notifications(
                    backend,
                    notification_callback,
                    attempt,
                )
                _LOGGER.debug(
                    "Eqiva %s: transport=%s connected backend=%s source=%s "
                    "notify_mode=%s",
                    self.address,
                    self.kind,
                    self._backend_name,
                    self._device_source or "unknown",
                    self._notify_mode,
                )
                return

            except BleakGATTProtocolError as err:
                last_error = err
                await self._abort()
                if (
                    err.code == BleakGATTProtocolErrorCode.UNLIKELY_ERROR
                    and attempt == 1
                ):
                    _LOGGER.warning(
                        "Eqiva %s: transport=%s GATT 0x0E; retrying once "
                        "without service cache after a new advertisement",
                        self.address,
                        self.kind,
                    )
                    continue
                raise EqivaConnectionError(
                    f"HA-GATT-Verbindung fehlgeschlagen ({type(err).__name__}: {err})"
                ) from err
            except Exception as err:
                last_error = err
                await self._abort()
                if attempt == 1 and "connection slot" in str(err).lower():
                    _LOGGER.warning(
                        "Eqiva %s: transport=%s connection slot unavailable; "
                        "retrying once after a new advertisement",
                        self.address,
                        self.kind,
                    )
                    continue
                if isinstance(err, (EqivaConnectionError, EqivaNotFoundError)):
                    raise
                raise EqivaConnectionError(
                    f"HA-GATT-Verbindung fehlgeschlagen ({type(err).__name__}: {err})"
                ) from err

        raise EqivaConnectionError(
            f"HA-GATT-Verbindung nach zwei Versuchen fehlgeschlagen: {last_error}"
        )

    async def session_ready(self) -> None:
        # KeyBLE authentication and BLE link security are separate. The
        # Home Assistant/Bleak path owns link security.
        return None

    async def write(self, data: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise EqivaConnectionError("HA-GATT-Transport ist nicht verbunden")
        if self._send_characteristic is None:
            raise EqivaConnectionError("Eqiva Send-Characteristic ist nicht verfügbar")
        try:
            # Eqiva uses the characteristic's Write Request path. Do not fall
            # back to write-without-response because that is an ATT Write Command.
            await client.write_gatt_char(self._send_characteristic, data, response=True)
        except Exception as err:
            raise EqivaConnectionError(
                "HA-GATT Write Request fehlgeschlagen oder ohne rechtzeitige "
                f"ATT-Antwort geblieben ({type(err).__name__}: {err})"
            ) from err

    async def _abort(self) -> None:
        client = self._client
        self._client = None
        self._send_characteristic = None
        self._receive_characteristic = None
        if client is None or not client.is_connected:
            return
        self._suppress_disconnect_callback = True
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug(
                "Eqiva %s: HA-GATT abort cleanup failed",
                self.address,
                exc_info=True,
            )
        finally:
            self._suppress_disconnect_callback = False

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._send_characteristic = None
        self._receive_characteristic = None
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug(
                    "Eqiva %s: HA-GATT disconnect cleanup failed",
                    self.address,
                    exc_info=True,
                )
