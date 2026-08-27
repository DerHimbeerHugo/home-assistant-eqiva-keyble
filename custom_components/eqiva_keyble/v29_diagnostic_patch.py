from __future__ import annotations

from typing import Any

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from .protocol import EqivaKeyBleClient, EqivaProtocolError
from .raw_att_client import EqivaRawATTClient

_RAW_MARKER = "RAW-PDU-v29"

# v29 deliberately abandons the SMP/CCCD-Write-Request experiments from v24-v28.
# Keep the proven raw ATT + MTU 23 path, then determine whether KeyBLE receives
# notifications without touching the CCCD at all. If the first connection cannot
# receive CONNECTION_INFO, bluez_notify_patch retries once and the second backend
# falls back to the known-working CCCD Write Command.
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER

_ORIGINAL_PREPARE_NOTIFY = EqivaRawATTClient.prepare_notify
_ORIGINAL_ENABLE_PREPARED_NOTIFY = EqivaRawATTClient.enable_prepared_notify

_ENABLE_ATTEMPTS: dict[str, int] = {}
_GATT_PROFILES: dict[str, str] = {}
_NOTIFY_MODES: dict[str, str] = {}


def _descriptor_text(characteristic: Any) -> str:
    descriptors = getattr(characteristic, "descriptors", None) or []
    if not descriptors:
        return "none"
    return ",".join(
        f"0x{descriptor.handle:04x}:{descriptor.uuid}"
        for descriptor in descriptors
    )


def _gatt_profile_text(characteristic: Any) -> str:
    properties = ",".join(sorted(getattr(characteristic, "properties", None) or []))
    return (
        f"rx_handle=0x{characteristic.handle:04x}; "
        f"props=[{properties}]; descriptors=[{_descriptor_text(characteristic)}]"
    )


def _prepare_notify_v29(self: EqivaRawATTClient, characteristic, callback) -> None:
    _ORIGINAL_PREPARE_NOTIFY(self, characteristic, callback)
    profile = _gatt_profile_text(characteristic)
    _GATT_PROFILES[self.address] = profile
    self._eqiva_gatt_profile = profile
    self._trace_note(f"GATT:{profile}")


async def _enable_prepared_notify_v29(
    self: EqivaRawATTClient, characteristic
) -> None:
    count = _ENABLE_ATTEMPTS.get(self.address, 0) + 1
    _ENABLE_ATTEMPTS[self.address] = count

    if count == 1:
        mode = "local-only"
        _NOTIFY_MODES[self.address] = mode
        self._eqiva_notify_mode = mode
        self._trace_note("NOTIFY:local-only(no-CCCD-write)")
        return

    mode = "cccd-command"
    _NOTIFY_MODES[self.address] = mode
    self._eqiva_notify_mode = mode
    self._trace_note("NOTIFY:cccd-write-command")
    await _ORIGINAL_ENABLE_PREPARED_NOTIFY(self, characteristic)


async def _confirm_prepared_notify_v29(
    self: EqivaRawATTClient, characteristic
) -> None:
    """v29 intentionally does not issue a protected CCCD Write Request."""
    self._trace_note("CCCD:write-request-skipped-v29")


def _remember_stage_v29(self: EqivaKeyBleClient, value: str) -> str:
    if "Nonce steht; geschützten CCCD Write Request testen" in value:
        value = f"{_RAW_MARKER}: Nonce steht; CCCD Write Request/SMP in v29 bewusst übersprungen"
    elif "KeyBLE Nonce + BLE Pairing + CCCD bestätigt" in value:
        value = f"{_RAW_MARKER}: KeyBLE Nonce abgeschlossen; v29 ohne SMP/CCCD-Write-Request"
    self._eqiva_raw_stage = value
    return value


def _backend_diag(self: EqivaKeyBleClient) -> tuple[str, str, str]:
    mode = _NOTIFY_MODES.get(self.address, "unbekannt")
    profile = _GATT_PROFILES.get(self.address, "unbekannt")
    trace = "raw backend nicht mehr verfügbar"
    client = getattr(self, "_client", None)
    backend = getattr(client, "_backend", None) if client is not None else None
    if isinstance(backend, EqivaRawATTClient):
        mode = getattr(backend, "_eqiva_notify_mode", mode)
        profile = getattr(backend, "_eqiva_gatt_profile", profile)
        trace = backend.trace_summary()
    return mode, profile, trace


async def _request_status_v29(self: EqivaKeyBleClient):
    try:
        return await _secure_trace_patch._ORIGINAL_REQUEST_STATUS(self)
    except EqivaProtocolError as err:
        if "ANSWER_WITHOUT_SECURITY" not in str(err):
            raise
        mode, profile, trace = _backend_diag(self)
        raise EqivaProtocolError(
            f"{_RAW_MARKER}: STATUS_REQUEST erhielt ANSWER_WITHOUT_SECURITY. "
            "Das Payloadbyte ist im Original-KeyBLE nur als Bitfeld bekannt und kein dokumentierter Fehlercode. "
            f"notify_mode={mode}; GATT={profile}; ATT-Spur={trace}; "
            f"Secure-TX: {_secure_trace_patch._secure_tx_text(self)}"
        ) from err


EqivaRawATTClient.prepare_notify = _prepare_notify_v29
EqivaRawATTClient.enable_prepared_notify = _enable_prepared_notify_v29
EqivaRawATTClient.confirm_prepared_notify = _confirm_prepared_notify_v29
_transport_patch._remember_stage = _remember_stage_v29
EqivaKeyBleClient.request_status = _request_status_v29
