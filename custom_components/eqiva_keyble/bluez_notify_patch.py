from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from homeassistant.components import bluetooth
from habluetooth.channels.l2cap import can_use_l2cap
from habluetooth.client_mgmt import MgmtClientData
from habluetooth.usage import ORIGINAL_BLEAK_CLIENT

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID
from .protocol import (
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
    MSG_CONNECTION_INFO,
    MSG_CONNECTION_REQUEST,
)
from .raw_att_client import EqivaRawATTClient

_LOGGER = logging.getLogger(__name__)
_RAW_MARKER = "RAW-PDU-v20"


def _remember_stage(self: EqivaKeyBleClient, value: str) -> str:
    self._eqiva_raw_stage = value
    return value


def _trace_summary(client: Any) -> str:
    backend = getattr(client, "_backend", None) if client is not None else None
    if isinstance(backend, EqivaRawATTClient):
        return backend.trace_summary()
    return "raw backend nicht mehr verfügbar"


def _local_raw_path(self: EqivaKeyBleClient):
    """Return the best local hci scanner path for the lock."""
    paths = bluetooth.async_scanner_devices_by_address(
        self.hass, self.address, connectable=True
    )
    local_paths = [
        path
        for path in paths
        if path.scanner.adapter_idx is not None
        and path.scanner.adapter.startswith("hci")
        and isinstance(path.scanner.source, str)
        and path.scanner.source.count(":") == 5
    ]
    if not local_paths:
        return None
    return max(
        local_paths,
        key=lambda path: path.advertisement.rssi
        if path.advertisement.rssi is not None
        else -127,
    )


def _eqiva_on_disconnect(self: EqivaKeyBleClient, disconnected_client: Any) -> None:
    """Preserve the exact raw ATT stage and sanitized ATT trace on a BLE drop."""
    stage = getattr(self, "_eqiva_raw_stage", f"{_RAW_MARKER}: Stage unbekannt")
    client = disconnected_client or self._client
    trace = _trace_summary(client)

    _LOGGER.warning(
        "Eqiva %s disconnected during %s; sanitized ATT trace: %s",
        self.address,
        stage,
        trace,
    )

    self._client = None
    self._reset_gatt()
    self._reset_session()
    self._fail_waiters(
        EqivaConnectionError(
            f"{_RAW_MARKER}: Bluetooth-Verbindung getrennt während: {stage}; "
            f"ATT-Spur: {trace}"
        )
    )


async def _eqiva_connect_raw_att(self: EqivaKeyBleClient) -> None:
    """Connect to Eqiva over raw L2CAP/ATT, bypassing bluetoothd GATT/MTU."""
    if self._client is not None and self._client.is_connected:
        return

    self._reset_session()
    self._reset_gatt()

    if not can_use_l2cap():
        raise EqivaConnectionError(
            "Der Home-Assistant-Core-Container darf keinen raw L2CAP/ATT-Socket öffnen. "
            "Der Eqiva-spezifische MTU-Bypass ist auf diesem System daher nicht verfügbar."
        )

    last_error: Exception | None = None

    for attempt in range(1, 3):
        waiter = None
        backend: EqivaRawATTClient | None = None
        await self._clear_stale_connection()
        if attempt > 1:
            await asyncio.sleep(1.0)

        path = _local_raw_path(self)
        if path is None:
            raise EqivaNotFoundError(
                "Das Eqiva wurde gefunden, aber aktuell steht kein lokaler hci-Bluetooth-Adapter "
                "als raw-ATT-Verbindungspfad zur Verfügung."
            )

        scanner = path.scanner
        device = path.ble_device
        source = scanner.source
        stage = _remember_stage(
            self,
            f"{_RAW_MARKER}: Raw L2CAP/ATT verbinden und GATT ohne MTU-Exchange "
            f"auflösen (Versuch {attempt}/2)",
        )

        _LOGGER.debug(
            "Eqiva %s: %s raw ATT attempt %s via %s / %s (RSSI %s)",
            self.address,
            _RAW_MARKER,
            attempt,
            scanner.adapter,
            source,
            path.advertisement.rssi,
        )

        client = ORIGINAL_BLEAK_CLIENT(
            device,
            disconnected_callback=self._on_disconnect,
            backend=EqivaRawATTClient,
            timeout=10.0,
            client_data=MgmtClientData(
                adapter_address=source,
                scanner=scanner,
            ),
        )
        self._client = client

        try:
            candidate_backend = getattr(client, "_backend", None)
            backend_name = (
                f"{type(candidate_backend).__module__}.{type(candidate_backend).__name__}"
                if candidate_backend is not None
                else "None"
            )
            if type(candidate_backend) is not EqivaRawATTClient:
                raise EqivaConnectionError(
                    f"{_RAW_MARKER}: unerwartetes Bleak-Backend {backend_name}; "
                    "EqivaRawATTClient wurde nicht geladen"
                )
            backend = candidate_backend

            await client.connect()

            stage = _remember_stage(
                self,
                f"{_RAW_MARKER}: Raw ATT GATT-Characteristics auflösen; "
                f"backend={backend_name}; mtu={client.mtu_size}",
            )
            services = client.services
            self._send_characteristic = services.get_characteristic(
                SEND_CHARACTERISTIC_UUID
            )
            self._receive_characteristic = services.get_characteristic(
                RECEIVE_CHARACTERISTIC_UUID
            )
            if self._send_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic wurde im raw-ATT-GATT-Profil nicht gefunden"
                )
            if self._receive_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic wurde im raw-ATT-GATT-Profil nicht gefunden"
                )

            send_properties = set(self._send_characteristic.properties)
            receive_properties = set(self._receive_characteristic.properties)
            if "write" not in send_properties and "write-without-response" not in send_properties:
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic ist nicht beschreibbar: "
                    f"{sorted(send_properties)}"
                )
            if not ({"notify", "indicate"} & receive_properties):
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic unterstützt keine Notifications: "
                    f"{sorted(receive_properties)}"
                )

            # v20 intentionally keeps the v19 transport behavior. This build is
            # diagnostic: first identify the exact point and ATT metadata around
            # the disconnect before changing another protocol variable.
            self._write_with_response = True

            receive_characteristic = self._receive_characteristic

            def _raw_notify_callback(data: bytearray) -> None:
                current = self._receive_characteristic or receive_characteristic
                self._notification_callback(current, data)

            stage = _remember_stage(
                self,
                f"{_RAW_MARKER}: Notify-Handler lokal vorbereiten; "
                f"backend={backend_name}; mtu={client.mtu_size}",
            )
            backend.prepare_notify(receive_characteristic, _raw_notify_callback)

            stage = _remember_stage(
                self,
                f"{_RAW_MARKER}: CCCD als ATT Write Command VOR CONNECTION_REQUEST senden; "
                f"mtu={client.mtu_size}",
            )
            await backend.enable_prepared_notify(receive_characteristic)

            self._local_nonce = os.urandom(8)
            waiter = self._new_waiter(MSG_CONNECTION_INFO)
            stage = _remember_stage(
                self,
                f"{_RAW_MARKER}: CONNECTION_REQUEST als ATT Write Request NACH CCCD-Command senden; "
                f"mtu={client.mtu_size}",
            )
            await self._send_message(
                MSG_CONNECTION_REQUEST,
                bytes([self.user_id]) + self._local_nonce,
                secure=False,
            )

            stage = _remember_stage(
                self,
                f"{_RAW_MARKER}: auf KeyBLE CONNECTION_INFO über raw ATT warten",
            )
            try:
                await asyncio.wait_for(waiter, timeout=5.0)
            except asyncio.TimeoutError as err:
                raise EqivaHandshakeError(
                    f"{_RAW_MARKER}: CCCD Write Command wurde gesendet und CONNECTION_REQUEST "
                    "als ATT Write Request bestätigt, aber innerhalb von 5 Sekunden kam keine "
                    f"CONNECTION_INFO-Nonce. ATT-Spur: {backend.trace_summary()}"
                ) from err

            if self._remote_nonce is None:
                raise EqivaHandshakeError(
                    f"{_RAW_MARKER}: Raw ATT steht, aber das Schloss hat keine CONNECTION_INFO-Nonce "
                    f"geliefert. ATT-Spur: {backend.trace_summary()}"
                )

            _remember_stage(self, f"{_RAW_MARKER}: KeyBLE Nonce-Handshake abgeschlossen")
            _LOGGER.debug(
                "Eqiva %s: %s raw ATT + KeyBLE nonce handshake established at MTU %s; trace=%s",
                self.address,
                _RAW_MARKER,
                client.mtu_size,
                backend.trace_summary(),
            )
            return

        except EqivaProtocolError:
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            await self._abort_connection()
            raise
        except Exception as err:  # noqa: BLE001
            last_error = err
            trace = backend.trace_summary() if backend is not None else "kein raw ATT trace"
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            await self._abort_connection()
            await self._clear_stale_connection()
            if attempt == 1:
                _LOGGER.warning(
                    "Eqiva %s: %s failed (%s: %s); trace=%s; retrying raw ATT once",
                    self.address,
                    stage,
                    type(err).__name__,
                    err,
                    trace,
                )
                continue
            raise EqivaConnectionError(
                f"{stage} fehlgeschlagen ({type(err).__name__}: {err}); ATT-Spur: {trace}"
            ) from err

    raise EqivaConnectionError(
        f"{_RAW_MARKER}: Raw L2CAP/ATT-Verbindungsaufbau nach zwei Versuchen fehlgeschlagen: "
        f"{last_error}"
    )


async def _eqiva_ensure_nonces_exchanged(self: EqivaKeyBleClient) -> None:
    """The raw connect path already performs the nonce exchange."""
    if self._remote_nonce is not None and self._local_nonce is not None:
        return
    await self._connect()
    if self._remote_nonce is None or self._local_nonce is None:
        raise EqivaHandshakeError(
            f"{_RAW_MARKER}: Raw ATT steht, aber der KeyBLE-Nonce-Handshake wurde nicht abgeschlossen"
        )


EqivaKeyBleClient._on_disconnect = _eqiva_on_disconnect
EqivaKeyBleClient._connect = _eqiva_connect_raw_att
EqivaKeyBleClient._ensure_nonces_exchanged = _eqiva_ensure_nonces_exchanged
