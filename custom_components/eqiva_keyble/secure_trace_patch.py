from __future__ import annotations

from typing import Any

from .protocol import (
    EqivaKeyBleClient,
    EqivaProtocolError,
    _auth_value,
    _ceil_step,
    _crypt_data,
    _pad_end,
)

_RAW_MARKER = "RAW-PDU-v26"

_ORIGINAL_SEND_MESSAGE = EqivaKeyBleClient._send_message
_ORIGINAL_REQUEST_STATUS = EqivaKeyBleClient.request_status


def _remember_secure_tx(
    self: EqivaKeyBleClient,
    message_type: int,
    data: bytes,
) -> None:
    """Record one reproducible secure-frame diagnostic without the user key."""
    if (
        self.user_key is None
        or self._remote_nonce is None
        or self._local_nonce is None
    ):
        self._eqiva_last_secure_tx = None
        return

    padded = _pad_end(data, _ceil_step(len(data), 15, 8))
    counter = self._local_counter
    encrypted = _crypt_data(
        padded,
        message_type,
        self._remote_nonce,
        counter,
        self.user_key,
    )
    auth = _auth_value(
        padded,
        message_type,
        self._remote_nonce,
        counter,
        self.user_key,
    )
    payload = encrypted + counter.to_bytes(2, "big") + auth
    wire = bytes([message_type]) + payload
    chunks = [wire[index:index + 15] for index in range(0, len(wire), 15)] or [b""]
    frames: list[bytes] = []
    for index, chunk in enumerate(chunks):
        remaining = len(chunks) - index - 1
        status = remaining | (0x80 if index == 0 else 0)
        frames.append(bytes([status]) + _pad_end(chunk, 15))

    self._eqiva_last_secure_tx = {
        "message_type": message_type,
        "counter": counter,
        "data": data.hex(),
        "padded": padded.hex(),
        "local_nonce": self._local_nonce.hex(),
        "remote_nonce": self._remote_nonce.hex(),
        "frames": [frame.hex() for frame in frames],
    }


def _secure_tx_text(self: EqivaKeyBleClient) -> str:
    diag: Any = getattr(self, "_eqiva_last_secure_tx", None)
    if not isinstance(diag, dict):
        return "kein Secure-TX-Datensatz"

    frames = diag.get("frames")
    frame_text = ",".join(frames) if isinstance(frames, list) else "unbekannt"
    message_type = diag.get("message_type")
    type_text = f"0x{message_type:02x}" if isinstance(message_type, int) else "unbekannt"
    return (
        f"type={type_text}; counter={diag.get('counter', 'unbekannt')}; "
        f"data={diag.get('data', 'unbekannt')}; padded={diag.get('padded', 'unbekannt')}; "
        f"local_nonce={diag.get('local_nonce', 'unbekannt')}; "
        f"remote_nonce={diag.get('remote_nonce', 'unbekannt')}; frame={frame_text}"
    )


async def _eqiva_send_message_with_secure_trace(
    self: EqivaKeyBleClient,
    message_type: int,
    data: bytes,
    secure: bool,
) -> None:
    if secure:
        _remember_secure_tx(self, message_type, data)
    await _ORIGINAL_SEND_MESSAGE(self, message_type, data, secure)


async def _eqiva_request_status_with_secure_trace(self: EqivaKeyBleClient):
    try:
        return await _ORIGINAL_REQUEST_STATUS(self)
    except EqivaProtocolError as err:
        if "ANSWER_WITHOUT_SECURITY" not in str(err):
            raise
        raise EqivaProtocolError(
            f"{_RAW_MARKER}: sicherer STATUS_REQUEST wurde vom Schloss abgelehnt. "
            f"Secure-TX: {_secure_tx_text(self)}. "
            "Der User-Key wird absichtlich nicht ausgegeben."
        ) from err


EqivaKeyBleClient._send_message = _eqiva_send_message_with_secure_trace
EqivaKeyBleClient.request_status = _eqiva_request_status_with_secure_trace
