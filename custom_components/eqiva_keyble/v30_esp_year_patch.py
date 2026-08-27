from __future__ import annotations

import asyncio

from homeassistant.util import dt as dt_util

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from .protocol import (
    EqivaKeyBleClient,
    EqivaProtocolError,
    MSG_STATUS_INFO,
    MSG_STATUS_REQUEST,
)

_RAW_MARKER = "RAW-PDU-v30"

# Keep all diagnostics on one visible marker. v30 changes exactly one protocol
# detail compared with v29: STATUS_REQUEST's year byte is generated exactly like
# digaust/esphome-components-eqiva's C++ StatusRequestMessage::encode().
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER


def _esp_year_byte(full_year: int) -> int:
    """Emulate `(char)(tm_year - 2000)` from the ESPHome Eqiva component.

    Standard C tm_year is years since 1900. The component subtracts 2000 from
    that value and writes the resulting char to the wire. Masking to one byte
    reproduces the resulting wire value (2026 -> 0xAE).
    """
    tm_year = full_year - 1900
    return (tm_year - 2000) & 0xFF


async def _request_status_v30(self: EqivaKeyBleClient):
    waiter = self._new_waiter(MSG_STATUS_INFO)
    now = dt_util.now()
    year_byte = _esp_year_byte(now.year)
    data = bytes(
        [
            year_byte,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
        ]
    )
    self._eqiva_v30_status_data = data.hex()

    try:
        await self._send_message(
            MSG_STATUS_REQUEST,
            data,
            secure=True,
        )
        await asyncio.wait_for(waiter, timeout=5.0)
    except asyncio.TimeoutError as err:
        self._cancel_waiter(MSG_STATUS_INFO, waiter)
        mode, profile, trace = _v29_patch._backend_diag(self)
        raise EqivaProtocolError(
            f"{_RAW_MARKER}: ESPHome-Jahresbyte 0x{year_byte:02x} gesendet, aber keine "
            f"STATUS_INFO-Antwort innerhalb von 5 Sekunden. notify_mode={mode}; "
            f"GATT={profile}; ATT-Spur={trace}; "
            f"Secure-TX: {_secure_trace_patch._secure_tx_text(self)}"
        ) from err
    except Exception as err:
        self._cancel_waiter(MSG_STATUS_INFO, waiter)
        if "ANSWER_WITHOUT_SECURITY" in str(err):
            mode, profile, trace = _v29_patch._backend_diag(self)
            raise EqivaProtocolError(
                f"{_RAW_MARKER}: STATUS_REQUEST mit exakt emulierter ESPHome-Jahreskodierung "
                f"wurde weiterhin mit ANSWER_WITHOUT_SECURITY abgelehnt. "
                f"esp_year_byte=0x{year_byte:02x}; data={data.hex()}; "
                f"notify_mode={mode}; GATT={profile}; ATT-Spur={trace}; "
                f"Secure-TX: {_secure_trace_patch._secure_tx_text(self)}"
            ) from err
        raise

    if self.last_status is None:
        raise EqivaProtocolError(
            f"{_RAW_MARKER}: STATUS_INFO wurde empfangen, aber kein Status dekodiert"
        )
    return self.last_status


EqivaKeyBleClient.request_status = _request_status_v30
