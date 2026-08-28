from __future__ import annotations

from bleak import BleakError

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from .raw_att_client import EqivaRawATTClient

_RAW_MARKER = "RAW-PDU-v37"

_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER

# Original KeyBLE deliberately does not await the GATT write callback/promise:
# the lock's KeyBLE notification may arrive before the ATT Write Response.  The
# generic habluetooth ATT codec, by contrast, waits for 0x13 and poisons the
# whole channel when it times out.  Eqiva uses the protocol-level notification
# as the meaningful acknowledgement, so mirror the reference implementation:
# still send a real ATT Write Request (0x12), but do not create/wait for an ATT
# transaction future.
_ORIGINAL_WRITE_GATT_CHAR = EqivaRawATTClient.write_gatt_char
_ATT_WRITE_REQUEST = 0x12


async def _write_gatt_char_v37(
    self: EqivaRawATTClient,
    characteristic,
    data,
    response: bool,
) -> None:
    if not response:
        # Preserve normal Write Command behaviour for callers that explicitly
        # request no response (for example diagnostic CCCD handling).
        await _ORIGINAL_WRITE_GATT_CHAR(self, characteristic, data, response)
        return

    value = bytes(data)
    max_len = self.mtu_size - 3
    if len(value) > max_len:
        raise BleakError(
            f"value too long for an ATT write: {len(value)} > {max_len} "
            "(long writes are not supported)"
        )

    payload = (
        bytes([_ATT_WRITE_REQUEST])
        + characteristic.handle.to_bytes(2, "little")
        + value
    )
    self._trace_note(
        f"WRITE:fire-and-forget-request@0x{characteristic.handle:04x}"
    )
    await self._send_traced_pdu(payload)


EqivaRawATTClient.write_gatt_char = _write_gatt_char_v37
