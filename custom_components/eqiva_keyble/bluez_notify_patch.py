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
_RAW_MARKER = "RAW-PDU-v18"


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


def _eqiva_on_disconnect(self: EqivaKeyBleClient, _client: Any) -> None:
    """Treat a physical BLE drop as a connection error, not authentication."""
    self._client = None
    self._reset_gatt()
    self._reset_session()
    self._fail_waiters(
        EqivaConnectionError(
            "Bluetooth-Verbindung zum Eqiva wurde während der Kommunikation getrennt"
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
        stage = (
            f"{_RAW_MARKER}: Raw L2CAP/ATT verbinden und GATT ohne MTU-Exchange "
            f"auflösen (Versuch {attempt}/2)"
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
            backend = getattr(client, "_backend", None)
            backend_name = (
                f"{type(backend).__module__}.{type(backend).__name__}"
                if backend is not None
                else "None"
            )
            if type(backend) is not EqivaRawATTClient:
                raise EqivaConnectionError(
                    f"{_RAW_MARKER}: unerwartetes Bleak-Backend {backend_name}; "
                    "EqivaRawATTClient wurde nicht geladen"
                )

            await client.connect()

            stage = (
                f"{_RAW_MARKER}: Raw ATT GATT-Characteristics auflösen; "
                f"backend={backend_name}; mtu={client.mtu_size}"
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

            # Match the working ESP-IDF transport: Key-BLE characteristic writes
            # are ATT Write Requests with responses. MTU stays fixed at 23.
            self._write_with_response = True

            receive_characteristic = self._receive_characteristic

            def _raw_notify_callback(data: bytearray) -> None:
                current = self._receive_characteristic or receive_characteristic
                self._notification_callback(current, data)

            # ESP-IDF first calls esp_ble_gattc_register_for_notify(), which is a
            # local registration step. It then immediately calls init()/sendNonce().
            # The actual CCCD write follows later from the asynchronous notify
            # registration event. Reproduce that ordering explicitly.
            stage = (
                f"{_RAW_MARKER}: Notify-Handler lokal vorbereiten; "
                f"backend={backend_name}; mtu={client.mtu_size}"
            )
            backend.prepare_notify(receive_characteristic, _raw_notify_callback)

            self._local_nonce = os.urandom(8)
            waiter = self._new_waiter(MSG_CONNECTION_INFO)
            stage = (
                f"{_RAW_MARKER}: CONNECTION_REQUEST VOR CCCD als ATT Write Request senden; "
                f"user_id={self.user_id}; mtu={client.mtu_size}"
            )
            await self._send_message(
                MSG_CONNECTION_REQUEST,
                bytes([self.user_id]) + self._local_nonce,
                secure=False,
            )

            stage = (
                f"{_RAW_MARKER}: CCCD NACH CONNECTION_REQUEST als ATT Write Request aktivieren; "
                f"backend={backend_name}; mtu={client.mtu_size}"
            )
            await backend.enable_prepared_notify(receive_characteristic)

            stage = f"{_RAW_MARKER}: KeyBLE CONNECTION_INFO über raw ATT empfangen"
            try:
                await asyncio.wait_for(waiter, timeout=5.0)
            except asyncio.TimeoutError as err:
                raise EqivaHandshakeError(
                    f"{_RAW_MARKER}: CONNECTION_REQUEST und anschließender CCCD-Write wurden "
                    "bestätigt, aber das Schloss hat innerhalb von 5 Sekunden keine "
                    "CONNECTION_INFO-Nonce geliefert."
                ) from err

            if self._remote_nonce is None:
                raise EqivaHandshakeError(
                    f"{_RAW_MARKER}: Raw ATT steht, aber das Schloss hat keine CONNECTION_INFO-Nonce geliefert."
                )

            _LOGGER.debug(
                "Eqiva %s: %s raw ATT + KeyBLE nonce handshake established at MTU %s",
                self.address,
                _RAW_MARKER,
                client.mtu_size,
            )
            return

        except EqivaProtocolError:
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            await self._abort_connection()
            raise
        except Exception as err:  # noqa: BLE001
            last_error = err
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            await self._abort_connection()
            await self._clear_stale_connection()
            if attempt == 1:
                _LOGGER.warning(
                    "Eqiva %s: %s failed (%s: %s); retrying raw ATT once",
                    self.address,
                    stage,
                    type(err).__name__,
                    err,
                )
                continue
            raise EqivaConnectionError(
                f"{stage} fehlgeschlagen ({type(err).__name__}: {err})"
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
