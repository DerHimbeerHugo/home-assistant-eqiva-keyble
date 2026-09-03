from __future__ import annotations

import asyncio
import logging
from typing import Any

from habluetooth import BluetoothScanningMode
from habluetooth.central_manager import get_manager
from habluetooth.channels.l2cap import can_use_l2cap
from habluetooth.client_mgmt import MgmtClientData
from habluetooth.usage import ORIGINAL_BLEAK_CLIENT
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.models import BluetoothCallbackReplay
from homeassistant.core import HomeAssistant

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID
from .exceptions import EqivaConnectionError, EqivaNotFoundError
from .raw_att_client import EqivaRawATTClient
from .transport import (
    DisconnectCallback,
    EqivaTransport,
    NotificationCallback,
    TransportType,
)

_LOGGER = logging.getLogger(__name__)

_RAW_CONNECT_TIMEOUT = 15.0
_ADVERTISEMENT_TIMEOUT = 12.0


def is_local_raw_path(path: Any) -> bool:
    """Return whether a Home Assistant scanner path supports local raw ATT."""
    scanner = path.scanner
    return bool(
        scanner.adapter_idx is not None
        and isinstance(scanner.adapter, str)
        and scanner.adapter.startswith("hci")
        and isinstance(scanner.source, str)
        and scanner.source.count(":") == 5
    )


def local_raw_path(hass: HomeAssistant, address: str):
    """Return the strongest current local hci path, never a cached path."""
    paths = bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)
    local_paths = [path for path in paths if is_local_raw_path(path)]
    if not local_paths:
        return None
    return max(
        local_paths,
        key=lambda path: (
            path.advertisement.rssi if path.advertisement.rssi is not None else -127
        ),
    )


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.debug("Eqiva background active scan failed", exc_info=True)


class RawAttTransport(EqivaTransport):
    """Known-working local Linux raw L2CAP/ATT transport.

    This consolidates the effective v29 + v36 + v37 runtime path without
    changing its wire semantics: MTU 23, fresh local advertisement wake-up,
    local notification handler with delayed CCCD Write Command, and real ATT
    Write Requests that are sent without awaiting ATT Write Response.
    """

    kind = TransportType.RAW_ATT

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
    ) -> None:
        super().__init__(address, name)
        self.hass = hass
        self._client: Any | None = None
        self._send_characteristic: Any | None = None
        self._receive_characteristic: Any | None = None
        self._disconnected_callback: DisconnectCallback | None = None
        self._suppress_disconnect_callback = False

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    def _is_local_hci_source(self, source: str | None) -> bool:
        if not source:
            return False
        scanner = bluetooth.async_scanner_by_source(self.hass, source)
        if scanner is None:
            return False
        return bool(
            scanner.adapter_idx is not None
            and isinstance(scanner.adapter, str)
            and scanner.adapter.startswith("hci")
            and isinstance(scanner.source, str)
            and scanner.source.count(":") == 5
        )

    def _path_summary(self) -> str:
        path = local_raw_path(self.hass, self.address)
        if path is None:
            return "none"
        scanner = path.scanner
        rssi = path.advertisement.rssi
        return (
            "present("
            f"source={scanner.source},adapter={scanner.adapter},"
            f"adapter_idx={scanner.adapter_idx},"
            f"rssi={rssi if rssi is not None else 'unknown'}"
            ")"
        )

    async def _wait_for_fresh_local_advertisement(self) -> str:
        """Keep v36 wake-up synchronization on the next local radio packet."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        seen: asyncio.Future[tuple[str, int | None]] = loop.create_future()

        _LOGGER.debug(
            "Eqiva %s: transport=%s clearing static advertisement history; "
            "local_path=%s",
            self.address,
            self.kind,
            self._path_summary(),
        )
        bluetooth.async_clear_advertisement_history(self.hass, self.address)

        def _advertisement_received(service_info, _change) -> None:
            if seen.done():
                return
            source = getattr(service_info, "source", None)
            if not self._is_local_hci_source(source):
                return
            seen.set_result((source, getattr(service_info, "rssi", None)))

        unload = bluetooth.async_register_callback(
            self.hass,
            _advertisement_received,
            {"address": self.address},
            BluetoothScanningMode.ACTIVE,
            replay=BluetoothCallbackReplay.DISABLED,
        )
        scan_task = self.hass.async_create_task(
            bluetooth.async_request_active_scan(self.hass, duration=10.0),
            f"eqiva-raw-att-scan-{self.address}",
        )
        scan_task.add_done_callback(_consume_task_result)

        try:
            async with asyncio.timeout(_ADVERTISEMENT_TIMEOUT):
                source, rssi = await seen
        except TimeoutError as err:
            raise EqivaNotFoundError(
                "Innerhalb von 12 Sekunden wurde kein neues Eqiva-Advertisement "
                "von einem lokalen hci-Adapter empfangen"
            ) from err
        finally:
            unload()

        deadline = loop.time() + 0.75
        while loop.time() < deadline:
            if local_raw_path(self.hass, self.address) is not None:
                _LOGGER.debug(
                    "Eqiva %s: transport=%s fresh local advertisement "
                    "after %.3fs source=%s rssi=%s",
                    self.address,
                    self.kind,
                    loop.time() - started,
                    source,
                    rssi if rssi is not None else "unknown",
                )
                return source
            await asyncio.sleep(0.025)

        raise EqivaNotFoundError(
            f"Frisches lokales Eqiva-Advertisement über {source} empfangen, "
            "aber Home Assistant hat danach keinen aktuellen Raw-ATT-Pfad "
            "veröffentlicht"
        )

    async def _clear_stale_connection(self) -> None:
        from bleak_retry_connector import close_stale_connections_by_address

        try:
            await close_stale_connections_by_address(self.address)
        except Exception:
            _LOGGER.debug(
                "Eqiva %s: transport=%s stale connection cleanup failed",
                self.address,
                self.kind,
                exc_info=True,
            )

    def _handle_disconnect(self, disconnected_client: Any) -> None:
        if self._client is disconnected_client:
            self._client = None
        self._send_characteristic = None
        self._receive_characteristic = None
        if (
            not self._suppress_disconnect_callback
            and self._disconnected_callback is not None
        ):
            self._disconnected_callback()

    async def connect(
        self,
        notification_callback: NotificationCallback,
        disconnected_callback: DisconnectCallback,
    ) -> None:
        if self.is_connected:
            return
        if not can_use_l2cap():
            raise EqivaConnectionError(
                "Der Home-Assistant-Core-Container darf keinen Raw-L2CAP/ATT-"
                "Socket öffnen"
            )

        self._disconnected_callback = disconnected_callback
        await self._wait_for_fresh_local_advertisement()
        await self._clear_stale_connection()

        path = local_raw_path(self.hass, self.address)
        if path is None:
            raise EqivaNotFoundError(
                "Kein aktueller lokaler hci-Bluetooth-Pfad für Raw ATT verfügbar"
            )

        scanner = path.scanner
        device = path.ble_device
        mgmt = get_manager().get_bluez_mgmt_ctl()
        client = ORIGINAL_BLEAK_CLIENT(
            device,
            disconnected_callback=self._handle_disconnect,
            backend=EqivaRawATTClient,
            timeout=_RAW_CONNECT_TIMEOUT,
            client_data=MgmtClientData(
                adapter_address=scanner.source,
                scanner=scanner,
                adapter_idx=scanner.adapter_idx,
                mgmt=mgmt,
            ),
        )
        self._client = client
        backend: EqivaRawATTClient | None = None

        try:
            candidate_backend = getattr(client, "_backend", None)
            backend_name = (
                f"{type(candidate_backend).__module__}."
                f"{type(candidate_backend).__name__}"
                if candidate_backend is not None
                else "None"
            )
            if type(candidate_backend) is not EqivaRawATTClient:
                raise EqivaConnectionError(
                    f"Unerwartetes Raw-ATT-Backend {backend_name}"
                )
            backend = candidate_backend

            await client.connect()

            services = client.services
            self._send_characteristic = services.get_characteristic(
                SEND_CHARACTERISTIC_UUID
            )
            self._receive_characteristic = services.get_characteristic(
                RECEIVE_CHARACTERISTIC_UUID
            )
            if self._send_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic wurde im Raw-ATT-GATT-Profil "
                    "nicht gefunden"
                )
            if self._receive_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic wurde im Raw-ATT-GATT-Profil "
                    "nicht gefunden"
                )

            send_properties = set(self._send_characteristic.properties)
            receive_properties = set(self._receive_characteristic.properties)
            if not ({"write", "write-without-response"} & send_properties):
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic ist nicht beschreibbar: "
                    f"{sorted(send_properties)}"
                )
            if not ({"notify", "indicate"} & receive_properties):
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic unterstützt keine "
                    f"Notifications: {sorted(receive_properties)}"
                )

            def _raw_notification(data: bytearray) -> None:
                notification_callback(bytes(data))

            backend.prepare_notify(self._receive_characteristic, _raw_notification)
            await backend.enable_prepared_notify(self._receive_characteristic)
            _LOGGER.debug(
                "Eqiva %s: transport=%s connected backend=%s mtu=%s notify_mode=%s",
                self.address,
                self.kind,
                backend_name,
                client.mtu_size,
                backend.notify_mode,
            )
        except Exception as err:
            trace = (
                backend.trace_summary() if backend is not None else "no ATT metadata"
            )
            await self._abort()
            if isinstance(err, (EqivaConnectionError, EqivaNotFoundError)):
                raise
            raise EqivaConnectionError(
                f"Raw-ATT-Verbindung fehlgeschlagen "
                f"({type(err).__name__}: {err}); ATT-Metadaten: {trace}"
            ) from err

    async def session_ready(self) -> None:
        """Keep the effective v29 post-nonce link-security behavior unchanged."""
        backend = getattr(self._client, "_backend", None)
        if (
            isinstance(backend, EqivaRawATTClient)
            and self._receive_characteristic is not None
        ):
            await backend.confirm_prepared_notify(self._receive_characteristic)

    async def write(self, data: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise EqivaConnectionError("Raw-ATT-Transport ist nicht verbunden")
        if self._send_characteristic is None:
            raise EqivaConnectionError("Eqiva Send-Characteristic ist nicht verfügbar")
        try:
            await client.write_gatt_char(self._send_characteristic, data, response=True)
        except Exception as err:
            raise EqivaConnectionError(
                f"Raw-ATT-Schreiben fehlgeschlagen ({type(err).__name__}: {err})"
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
                "Eqiva %s: raw ATT abort cleanup failed",
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
                    "Eqiva %s: raw ATT disconnect cleanup failed",
                    self.address,
                    exc_info=True,
                )
