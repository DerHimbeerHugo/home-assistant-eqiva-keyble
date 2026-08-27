from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
import os
import re
from typing import Final

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID

_LOGGER = logging.getLogger(__name__)

MSG_FRAGMENT_ACK: Final = 0x00
MSG_ANSWER_WITHOUT_SECURITY: Final = 0x01
MSG_CONNECTION_REQUEST: Final = 0x02
MSG_CONNECTION_INFO: Final = 0x03
MSG_PAIRING_REQUEST: Final = 0x04
MSG_STATUS_CHANGED: Final = 0x05
MSG_CLOSE_CONNECTION: Final = 0x06
MSG_ANSWER_WITH_SECURITY: Final = 0x81
MSG_STATUS_REQUEST: Final = 0x82
MSG_STATUS_INFO: Final = 0x83
MSG_COMMAND: Final = 0x87

STATUS_UNKNOWN = 0
STATUS_MOVING = 1
STATUS_UNLOCKED = 2
STATUS_LOCKED = 3
STATUS_OPENED = 4

KEY_CARD_PATTERN: Final = re.compile(
    r"^M(?P<address>[0-9A-F]{12})K(?P<key>[0-9A-F]{32})(?P<serial>[0-9A-Z]{10})$"
)


class EqivaProtocolError(Exception):
    """Protocol or authentication error."""


class EqivaNotFoundError(EqivaProtocolError):
    """Bluetooth device is not currently known to Home Assistant."""


class EqivaConnectionError(EqivaProtocolError):
    """Bluetooth/GATT connection setup failed."""


class EqivaHandshakeError(EqivaProtocolError):
    """The Key-BLE nonce handshake failed."""


@dataclass(slots=True)
class EqivaStatus:
    lock_status: int
    battery_low: bool
    pairing_allowed: bool

    @property
    def is_locked(self) -> bool | None:
        if self.lock_status == STATUS_LOCKED:
            return True
        if self.lock_status in (STATUS_UNLOCKED, STATUS_OPENED):
            return False
        return None

    @property
    def is_moving(self) -> bool:
        return self.lock_status == STATUS_MOVING


@dataclass(slots=True)
class KeyCardData:
    address: str
    key: bytes
    serial: str


def parse_key_card(value: str) -> KeyCardData:
    normalized = value.strip().upper()
    match = KEY_CARD_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError("Ungültige Eqiva Key-Card-Daten")
    mac = ":".join(
        match.group("address")[index:index + 2] for index in range(0, 12, 2)
    )
    return KeyCardData(
        mac,
        bytes.fromhex(match.group("key")),
        match.group("serial"),
    )


def canonical_address(value: str) -> str:
    clean = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(clean) != 12:
        raise ValueError("Ungültige Bluetooth-Adresse")
    return ":".join(clean[index:index + 2] for index in range(0, 12, 2)).upper()


def canonical_key(value: str) -> bytes:
    clean = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(clean) != 32:
        raise ValueError("Der User Key muss 128 Bit / 32 Hex-Zeichen lang sein")
    return bytes.fromhex(clean)


def _ceil_step(value: int, step: int = 1, offset: int = 0) -> int:
    return math.ceil((value - offset) / step) * step + offset


def _pad_end(data: bytes, length: int) -> bytes:
    return data if len(data) >= length else data + bytes(length - len(data))


def _xor(a: bytes, b: bytes, offset: int = 0) -> bytes:
    return bytes(
        value ^ b[(index + offset) % len(b)] for index, value in enumerate(a)
    )


def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES ECB input must be block aligned")
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _nonce(message_type: int, session_nonce: bytes, counter: int) -> bytes:
    return (
        bytes([message_type])
        + session_nonce
        + b"\x00\x00"
        + counter.to_bytes(2, "big")
    )


def _crypt_data(
    data: bytes,
    message_type: int,
    session_nonce: bytes,
    counter: int,
    key: bytes,
) -> bytes:
    nonce = _nonce(message_type, session_nonce, counter)
    stream = bytearray()
    for index in range(math.ceil(len(data) / 16)):
        stream.extend(
            _aes_ecb_encrypt(
                b"\x01" + nonce + (index + 1).to_bytes(2, "big"), key
            )
        )
    return _xor(data, bytes(stream))


def _auth_value(
    data: bytes,
    message_type: int,
    session_nonce: bytes,
    counter: int,
    key: bytes,
) -> bytes:
    nonce = _nonce(message_type, session_nonce, counter)
    padded_len = _ceil_step(len(data), 16)
    padded = _pad_end(data, padded_len)
    encrypted = _aes_ecb_encrypt(
        b"\x09" + nonce + len(data).to_bytes(2, "big"), key
    )
    for offset in range(0, padded_len, 16):
        encrypted = _aes_ecb_encrypt(_xor(encrypted, padded, offset), key)
    return _xor(
        encrypted[:4],
        _aes_ecb_encrypt(b"\x01" + nonce + b"\x00\x00", key),
    )


class EqivaKeyBleClient:
    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        user_id: int = 255,
        user_key: bytes | None = None,
        name: str = "Eqiva Key-BLE",
    ) -> None:
        self.hass = hass
        self.address = canonical_address(address)
        self.user_id = user_id
        self.user_key = user_key
        self.name = name
        self._client: BleakClient | None = None
        self._send_characteristic: BleakGATTCharacteristic | None = None
        self._receive_characteristic: BleakGATTCharacteristic | None = None
        self._write_with_response = True
        self._received_fragments: list[bytes] = []
        self._waiters: dict[int, list[asyncio.Future[bytes]]] = {}
        self._operation_lock = asyncio.Lock()
        self._local_nonce: bytes | None = None
        self._remote_nonce: bytes | None = None
        self._local_counter = 1
        self._remote_counter = 0
        self.last_status: EqivaStatus | None = None

    def _reset_session(self) -> None:
        self._received_fragments.clear()
        self._local_nonce = None
        self._remote_nonce = None
        self._local_counter = 1
        self._remote_counter = 0

    def _reset_gatt(self) -> None:
        self._send_characteristic = None
        self._receive_characteristic = None
        self._write_with_response = True

    def _fresh_ble_device(self):
        return async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _clear_stale_connection(self) -> None:
        """Clear a half-open BlueZ/proxy connection to this lock only."""
        try:
            await close_stale_connections_by_address(self.address)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Eqiva %s: stale-connection cleanup failed",
                self.address,
                exc_info=True,
            )

    async def _connect(self) -> None:
        if self._client is not None and self._client.is_connected:
            return

        self._reset_session()
        self._reset_gatt()
        last_error: Exception | None = None

        for attempt in range(1, 3):
            await self._clear_stale_connection()
            if attempt > 1:
                await asyncio.sleep(1.5)

            device = self._fresh_ble_device()
            if device is None:
                raise EqivaNotFoundError(
                    f"{self.address} wurde von Home Assistant Bluetooth noch nicht gefunden"
                )

            stage = f"BLE-Verbindung / GATT-Serviceauflösung (Versuch {attempt}/2)"
            _LOGGER.debug("Eqiva %s: starting %s", self.address, stage)
            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=1,
                    use_services_cache=attempt == 1,
                )
                _LOGGER.debug("Eqiva %s: BLE connection established", self.address)

                stage = "GATT-Characteristics auflösen"
                services = self._client.services
                self._send_characteristic = services.get_characteristic(
                    SEND_CHARACTERISTIC_UUID
                )
                self._receive_characteristic = services.get_characteristic(
                    RECEIVE_CHARACTERISTIC_UUID
                )
                if self._send_characteristic is None:
                    raise EqivaConnectionError(
                        "Eqiva Send-Characteristic wurde im GATT-Profil nicht gefunden"
                    )
                if self._receive_characteristic is None:
                    raise EqivaConnectionError(
                        "Eqiva Receive-Characteristic wurde im GATT-Profil nicht gefunden"
                    )

                send_properties = set(self._send_characteristic.properties)
                receive_properties = set(self._receive_characteristic.properties)
                _LOGGER.debug(
                    "Eqiva %s: GATT send properties=%s receive properties=%s",
                    self.address,
                    sorted(send_properties),
                    sorted(receive_properties),
                )

                if "write" in send_properties:
                    self._write_with_response = True
                elif "write-without-response" in send_properties:
                    self._write_with_response = False
                else:
                    raise EqivaConnectionError(
                        "Eqiva Send-Characteristic ist nicht beschreibbar: "
                        f"{sorted(send_properties)}"
                    )

                if not ({"notify", "indicate"} & receive_properties):
                    raise EqivaConnectionError(
                        "Eqiva Receive-Characteristic unterstützt keine Notifications: "
                        f"{sorted(receive_properties)}"
                    )

                # Give the lock a short quiet period after service discovery before
                # touching the CCCD/notification path. On local BlueZ, Bleak 3 can
                # use either AcquireNotify or StartNotify. Eqiva/eQ-3 devices are
                # known to be unusually sensitive around notification setup, so try
                # the alternative BlueZ path first and keep the standard path as the
                # second clean-connection attempt.
                await asyncio.sleep(0.25)
                backend_id = str(getattr(self._client, "backend_id", "")).lower()
                use_acquire_notify = "bluez" in backend_id and attempt == 1
                notify_mode = "AcquireNotify" if use_acquire_notify else "StartNotify"
                stage = f"Notifications aktivieren ({notify_mode})"
                _LOGGER.debug(
                    "Eqiva %s: enabling notifications using %s (backend=%s)",
                    self.address,
                    notify_mode,
                    backend_id or "unknown",
                )
                notify_kwargs = (
                    {"bluez": {"use_start_notify": False}}
                    if use_acquire_notify
                    else {}
                )
                await self._client.start_notify(
                    self._receive_characteristic,
                    self._notification_callback,
                    **notify_kwargs,
                )
                _LOGGER.debug(
                    "Eqiva %s: notifications enabled via %s; write_with_response=%s",
                    self.address,
                    notify_mode,
                    self._write_with_response,
                )
                return

            except EqivaProtocolError:
                await self._abort_connection()
                raise
            except BleakGATTProtocolError as err:
                last_error = err
                await self._abort_connection()
                await self._clear_stale_connection()
                if (
                    err.code == BleakGATTProtocolErrorCode.UNLIKELY_ERROR
                    and attempt == 1
                ):
                    _LOGGER.warning(
                        "Eqiva %s: GATT 0x0E during %s; retrying with a fresh connection without service cache",
                        self.address,
                        stage,
                    )
                    continue
                raise EqivaConnectionError(
                    f"{stage} fehlgeschlagen ({type(err).__name__}: {err})"
                ) from err
            except Exception as err:
                last_error = err
                await self._abort_connection()
                await self._clear_stale_connection()
                if attempt == 1 and "connection slot" in str(err).lower():
                    _LOGGER.warning(
                        "Eqiva %s: connection slot unavailable during %s; retrying after stale cleanup",
                        self.address,
                        stage,
                    )
                    continue
                raise EqivaConnectionError(
                    f"{stage} fehlgeschlagen ({type(err).__name__}: {err})"
                ) from err

        raise EqivaConnectionError(
            "BLE-/GATT-Verbindungsaufbau nach zwei Versuchen fehlgeschlagen: "
            f"{last_error}"
        )

    async def _abort_connection(self) -> None:
        client = self._client
        self._client = None
        self._reset_gatt()
        self._reset_session()
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Eqiva %s: disconnect after connection error failed",
                    self.address,
                    exc_info=True,
                )

    async def _disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            if client.is_connected:
                try:
                    await self._send_message(
                        MSG_CLOSE_CONNECTION, b"", secure=False
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "Eqiva %s: CLOSE_CONNECTION could not be sent",
                        self.address,
                        exc_info=True,
                    )
                if client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Eqiva %s: BLE disconnect cleanup failed",
                            self.address,
                            exc_info=True,
                        )
        finally:
            if self._client is client:
                self._client = None
            self._reset_gatt()
            self._reset_session()

    def _fail_waiters(self, error: Exception) -> None:
        for waiters in self._waiters.values():
            for future in waiters:
                if not future.done():
                    future.set_exception(error)
        self._waiters.clear()

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        self._reset_gatt()
        self._reset_session()
        self._fail_waiters(EqivaProtocolError("Bluetooth-Verbindung getrennt"))

    def _notification_callback(
        self,
        _characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        try:
            self._handle_fragment(bytes(data))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Fehler beim Verarbeiten einer Eqiva-BLE-Nachricht")
            self._fail_waiters(err)

    def _new_waiter(self, message_type: int) -> asyncio.Future[bytes]:
        future: asyncio.Future[bytes] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiters.setdefault(message_type, []).append(future)
        return future

    def _resolve_waiter(self, message_type: int, data: bytes) -> None:
        queue = self._waiters.get(message_type)
        if not queue:
            return
        future = queue.pop(0)
        if not queue:
            self._waiters.pop(message_type, None)
        if not future.done():
            future.set_result(data)

    def _cancel_waiter(
        self,
        message_type: int,
        future: asyncio.Future[bytes],
    ) -> None:
        queue = self._waiters.get(message_type)
        if queue and future in queue:
            queue.remove(future)
            if not queue:
                self._waiters.pop(message_type, None)
        if not future.done():
            future.cancel()

    def _handle_fragment(self, fragment: bytes) -> None:
        if len(fragment) < 2:
            return
        status = fragment[0]
        is_first = bool(status & 0x80)
        remaining = status & 0x7F
        if is_first:
            self._received_fragments = [fragment]
        else:
            if not self._received_fragments:
                return
            self._received_fragments.append(fragment)

        if remaining:
            self.hass.async_create_task(
                self._send_message(
                    MSG_FRAGMENT_ACK,
                    bytes([status]),
                    secure=False,
                ),
                "Eqiva fragment ACK",
            )
            return

        fragments = self._received_fragments
        self._received_fragments = []
        first = fragments[0]
        message_type = first[1]
        payload = first[2:] + b"".join(
            part[1:] for part in fragments[1:]
        )
        _LOGGER.debug(
            "Eqiva %s: received message type 0x%02X",
            self.address,
            message_type,
        )

        if message_type & 0x80:
            if self.user_key is None or self._local_nonce is None:
                raise EqivaProtocolError(
                    "Sichere Nachricht ohne Session-Schlüssel empfangen"
                )
            if len(payload) < 6:
                raise EqivaProtocolError("Sichere Nachricht ist zu kurz")
            counter = int.from_bytes(payload[-6:-4], "big")
            if counter <= self._remote_counter:
                raise EqivaProtocolError("Ungültiger Security Counter")
            encrypted = payload[:-6]
            auth = payload[-4:]
            plain = _crypt_data(
                encrypted,
                message_type,
                self._local_nonce,
                counter,
                self.user_key,
            )
            expected = _auth_value(
                plain,
                message_type,
                self._local_nonce,
                counter,
                self.user_key,
            )
            if auth != expected:
                raise EqivaProtocolError(
                    "Authentifizierung der BLE-Nachricht fehlgeschlagen"
                )
            self._remote_counter = counter
            payload = plain

        if message_type == MSG_CONNECTION_INFO:
            if len(payload) < 9:
                raise EqivaProtocolError("CONNECTION_INFO ist zu kurz")
            self.user_id = payload[0]
            self._remote_nonce = payload[1:9]
            self._local_counter = 1
            self._remote_counter = 0
        elif message_type == MSG_STATUS_INFO and len(payload) >= 3:
            self.last_status = EqivaStatus(
                lock_status=payload[2] & 0x07,
                battery_low=bool(payload[1] & 0x80),
                pairing_allowed=bool(payload[1] & 0x01),
            )

        self._resolve_waiter(message_type, payload)

    async def _write_fragment(
        self,
        fragment: bytes,
        wait_for_ack: bool,
    ) -> None:
        if self._client is None or not self._client.is_connected:
            raise EqivaConnectionError("Nicht mit dem Schloss verbunden")
        if self._send_characteristic is None:
            raise EqivaConnectionError(
                "Eqiva Send-Characteristic ist nicht verfügbar"
            )
        waiter = (
            self._new_waiter(MSG_FRAGMENT_ACK) if wait_for_ack else None
        )
        try:
            await self._client.write_gatt_char(
                self._send_characteristic,
                fragment,
                response=self._write_with_response,
            )
            if waiter is not None:
                await asyncio.wait_for(waiter, timeout=3.0)
        except Exception as err:
            if waiter is not None:
                self._cancel_waiter(MSG_FRAGMENT_ACK, waiter)
            if isinstance(err, EqivaProtocolError):
                raise
            raise EqivaConnectionError(
                "Schreiben auf die Eqiva GATT-Characteristic fehlgeschlagen "
                f"({type(err).__name__}: {err})"
            ) from err

    async def _send_message(
        self,
        message_type: int,
        data: bytes,
        secure: bool,
    ) -> None:
        if secure:
            await self._ensure_nonces_exchanged()
            if self.user_key is None or self._remote_nonce is None:
                raise EqivaProtocolError("User Key oder Remote Nonce fehlt")
            padded = _pad_end(data, _ceil_step(len(data), 15, 8))
            counter = self._local_counter
            encrypted = _crypt_data(
                padded,
                message_type,
                self._remote_nonce,
                counter,
                self.user_key,
            )
            payload = (
                encrypted
                + counter.to_bytes(2, "big")
                + _auth_value(
                    padded,
                    message_type,
                    self._remote_nonce,
                    counter,
                    self.user_key,
                )
            )
            self._local_counter += 1
        else:
            if self._client is None or not self._client.is_connected:
                await self._connect()
            payload = data

        wire = bytes([message_type]) + payload
        chunks = [
            wire[index:index + 15]
            for index in range(0, len(wire), 15)
        ] or [b""]
        for index, chunk in enumerate(chunks):
            remaining = len(chunks) - index - 1
            status = remaining | (0x80 if index == 0 else 0)
            fragment = bytes([status]) + _pad_end(chunk, 15)
            await self._write_fragment(
                fragment,
                wait_for_ack=remaining > 0,
            )

    async def _ensure_nonces_exchanged(self) -> None:
        if self._remote_nonce is not None and self._local_nonce is not None:
            return
        await self._connect()
        self._local_nonce = os.urandom(8)
        waiter = self._new_waiter(MSG_CONNECTION_INFO)
        _LOGGER.debug(
            "Eqiva %s: sending CONNECTION_REQUEST for user id %s",
            self.address,
            self.user_id,
        )
        try:
            await self._send_message(
                MSG_CONNECTION_REQUEST,
                bytes([self.user_id]) + self._local_nonce,
                secure=False,
            )
            await asyncio.wait_for(waiter, timeout=5.0)
        except asyncio.TimeoutError as err:
            self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            raise EqivaHandshakeError(
                "CONNECTION_INFO wurde nicht innerhalb von 5 Sekunden empfangen. "
                "Das Schloss kann bereits durch eine andere Bluetooth-Verbindung belegt sein."
            ) from err
        except Exception:
            self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            raise
        if self._remote_nonce is None:
            raise EqivaHandshakeError(
                "Keine Session-Nonce vom Schloss erhalten"
            )
        _LOGGER.debug("Eqiva %s: nonce handshake completed", self.address)

    async def pair(self, card_key: bytes) -> tuple[int, bytes]:
        if len(card_key) != 16:
            raise ValueError("Card Key muss 16 Byte lang sein")
        async with self._operation_lock:
            self.user_id = 255
            self.user_key = os.urandom(16)
            try:
                await self._ensure_nonces_exchanged()
                if self._remote_nonce is None:
                    raise EqivaProtocolError("Remote Nonce fehlt")
                counter = self._local_counter
                encrypted_pair_key = _crypt_data(
                    self.user_key,
                    MSG_PAIRING_REQUEST,
                    self._remote_nonce,
                    counter,
                    card_key,
                )
                auth_data = _pad_end(
                    bytes([self.user_id]) + self.user_key,
                    23,
                )
                auth = _auth_value(
                    auth_data,
                    MSG_PAIRING_REQUEST,
                    self._remote_nonce,
                    counter,
                    card_key,
                )
                payload = (
                    bytes([self.user_id])
                    + _pad_end(encrypted_pair_key, 22)
                    + counter.to_bytes(2, "big")
                    + auth
                )
                waiter = self._new_waiter(MSG_ANSWER_WITH_SECURITY)
                try:
                    await self._send_message(
                        MSG_PAIRING_REQUEST,
                        payload,
                        secure=False,
                    )
                    await asyncio.wait_for(waiter, timeout=10.0)
                except asyncio.TimeoutError as err:
                    self._cancel_waiter(
                        MSG_ANSWER_WITH_SECURITY,
                        waiter,
                    )
                    raise EqivaProtocolError(
                        "Keine Pairing-Antwort innerhalb von 10 Sekunden empfangen. "
                        "Prüfe, ob die gelbe Pairing-LED am Schloss blinkt."
                    ) from err
                except Exception:
                    self._cancel_waiter(
                        MSG_ANSWER_WITH_SECURITY,
                        waiter,
                    )
                    raise
                return self.user_id, self.user_key
            finally:
                await self._disconnect()

    async def request_status(self) -> EqivaStatus:
        waiter = self._new_waiter(MSG_STATUS_INFO)
        now = dt_util.now()
        data = bytes(
            [
                now.year - 2000,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
            ]
        )
        try:
            await self._send_message(
                MSG_STATUS_REQUEST,
                data,
                secure=True,
            )
            await asyncio.wait_for(waiter, timeout=5.0)
        except asyncio.TimeoutError as err:
            self._cancel_waiter(MSG_STATUS_INFO, waiter)
            raise EqivaProtocolError(
                "Keine STATUS_INFO-Antwort innerhalb von 5 Sekunden empfangen. "
                "Bei vorhandenen Zugangsdaten kann dies auf eine falsche User-ID "
                "oder einen falschen User-Key hindeuten."
            ) from err
        except Exception:
            self._cancel_waiter(MSG_STATUS_INFO, waiter)
            raise
        if self.last_status is None:
            raise EqivaProtocolError("Kein Status empfangen")
        return self.last_status

    async def status(self) -> EqivaStatus:
        async with self._operation_lock:
            try:
                await self._connect()
                return await self.request_status()
            finally:
                await self._disconnect()

    async def _command(
        self,
        command: int,
        targets: set[int],
    ) -> EqivaStatus:
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
