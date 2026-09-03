from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from custom_components.eqiva_keyble.exceptions import EqivaConnectionError
from custom_components.eqiva_keyble.protocol import (
    MSG_COMMAND,
    MSG_CONNECTION_INFO,
    MSG_CONNECTION_REQUEST,
    MSG_FRAGMENT_ACK,
    MSG_STATUS_INFO,
    STATUS_LOCKED,
    EqivaKeyBleClient,
    _auth_value,
    _crypt_data,
)
from custom_components.eqiva_keyble.retrying_client import EqivaRetryingKeyBleClient
from custom_components.eqiva_keyble.transport import (
    DisconnectCallback,
    EqivaTransport,
    NotificationCallback,
    TransportType,
)


class FakeHass:
    def async_create_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine, name=name)


class FakeTransport(EqivaTransport):
    kind = TransportType.HA_GATT

    def __init__(
        self,
        *,
        fail_connects: int = 0,
        fail_command_write: bool = False,
    ) -> None:
        super().__init__("00:11:22:33:44:55", "Test lock")
        self.connected = False
        self.fail_connects = fail_connects
        self.fail_command_write = fail_command_write
        self.connect_calls = 0
        self.command_writes = 0
        self.session_ready_calls = 0
        self.writes: list[bytes] = []
        self.notification_callback: NotificationCallback | None = None
        self.disconnected_callback: DisconnectCallback | None = None

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(
        self,
        notification_callback: NotificationCallback,
        disconnected_callback: DisconnectCallback,
    ) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connects:
            raise EqivaConnectionError("synthetic pre-command connect failure")
        self.notification_callback = notification_callback
        self.disconnected_callback = disconnected_callback
        self.connected = True

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        message_type = data[1] if data[0] & 0x80 else None
        if message_type == MSG_CONNECTION_REQUEST:
            assert self.notification_callback is not None
            self.notification_callback(
                bytes([0x80, MSG_CONNECTION_INFO, 7]) + b"remote!!" + bytes(5)
            )
        elif message_type == MSG_COMMAND:
            self.command_writes += 1
            if self.fail_command_write:
                raise EqivaConnectionError("synthetic ambiguous command write timeout")

        if data[0] & 0x7F:
            assert self.notification_callback is not None
            self.notification_callback(bytes([0x80, MSG_FRAGMENT_ACK]) + bytes(14))

    async def session_ready(self) -> None:
        self.session_ready_calls += 1

    async def disconnect(self) -> None:
        self.connected = False


def _client(transport: FakeTransport) -> EqivaKeyBleClient:
    return EqivaKeyBleClient(
        FakeHass(),
        transport.address,
        user_id=7,
        user_key=bytes.fromhex("00112233445566778899aabbccddeeff"),
        transport=transport,
    )


def test_crypto_round_trip_and_authentication_vector() -> None:
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    nonce = bytes.fromhex("0102030405060708")
    plain = bytes.fromhex("0001020304050607")

    encrypted = _crypt_data(plain, MSG_COMMAND, nonce, 1, key)

    assert encrypted.hex() == "4cebce4b0153ee41"
    assert _crypt_data(encrypted, MSG_COMMAND, nonce, 1, key) == plain
    assert _auth_value(plain, MSG_COMMAND, nonce, 1, key).hex() == "e424f045"


@pytest.mark.asyncio
async def test_outgoing_fragmentation_and_protocol_ack() -> None:
    transport = FakeTransport()
    client = _client(transport)
    transport.connected = True
    transport.notification_callback = client._notification_callback

    await client._send_message(
        MSG_CONNECTION_REQUEST,
        bytes(range(31)),
        secure=False,
    )

    assert [fragment[0] for fragment in transport.writes] == [0x82, 0x01, 0x00]
    assert all(len(fragment) == 16 for fragment in transport.writes)


@pytest.mark.asyncio
async def test_incoming_fragment_reassembly() -> None:
    transport = FakeTransport()
    client = _client(transport)
    transport.connected = True
    transport.notification_callback = client._notification_callback
    waiter = client._new_waiter(MSG_CONNECTION_INFO)
    payload = bytes([7]) + b"remote!!" + b"fragmented"
    wire = bytes([MSG_CONNECTION_INFO]) + payload
    chunks = [wire[index : index + 15] for index in range(0, len(wire), 15)]

    client._handle_fragment(bytes([0x81]) + chunks[0])
    client._handle_fragment(bytes([0x00]) + chunks[1].ljust(15, b"\x00"))
    received = await waiter
    await asyncio.sleep(0)

    assert received.startswith(payload)
    assert client._remote_nonce == b"remote!!"
    assert any(fragment[1] == MSG_FRAGMENT_ACK for fragment in transport.writes)


@pytest.mark.asyncio
async def test_nonce_session_is_transport_independent() -> None:
    transport = FakeTransport()
    client = _client(transport)

    await client._ensure_nonces_exchanged()

    assert client.user_id == 7
    assert client._remote_nonce == b"remote!!"
    assert transport.connect_calls == 1
    assert transport.session_ready_calls == 1


def test_secure_status_message_processing() -> None:
    transport = FakeTransport()
    client = _client(transport)
    client._local_nonce = bytes.fromhex("0102030405060708")
    plain = bytes([0x00, 0x81, STATUS_LOCKED]) + bytes(5)
    counter = 1
    encrypted = _crypt_data(
        plain,
        MSG_STATUS_INFO,
        client._local_nonce,
        counter,
        client.user_key,
    )
    auth = _auth_value(
        plain,
        MSG_STATUS_INFO,
        client._local_nonce,
        counter,
        client.user_key,
    )
    fragment = (
        bytes([0x80, MSG_STATUS_INFO]) + encrypted + counter.to_bytes(2, "big") + auth
    )

    client._handle_fragment(fragment)

    assert client.last_status is not None
    assert client.last_status.lock_status == STATUS_LOCKED
    assert client.last_status.battery_low is True
    assert client.last_status.pairing_allowed is True


@pytest.mark.asyncio
async def test_motor_command_is_not_repeated_after_ambiguous_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.eqiva_keyble import retrying_client

    monkeypatch.setattr(retrying_client, "_CONNECTION_RETRY_DELAY", 0)
    transport = FakeTransport(
        fail_connects=1,
        fail_command_write=True,
    )
    client = EqivaRetryingKeyBleClient(
        FakeHass(),
        transport.address,
        user_id=7,
        user_key=bytes.fromhex("00112233445566778899aabbccddeeff"),
        transport=transport,
    )

    with pytest.raises(EqivaConnectionError):
        await client.lock()

    assert transport.connect_calls == 2
    assert transport.command_writes == 1
