from __future__ import annotations

import asyncio

from bleak import BleakError
from habluetooth.channels.bluez import (
    AuthenticationFailed,
    NewLongTermKey,
    UserConfirmationRequest,
    UserPasskeyRequest,
)
from habluetooth.channels.l2cap import BT_SECURITY_MEDIUM

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from .raw_att_client import EqivaRawATTClient

_RAW_MARKER = "RAW-PDU-v28"

# BlueZ management protocol constants used only for this narrow Eqiva test.
_MGMT_OP_READ_INFO = 0x0004
_MGMT_OP_SET_BONDABLE = 0x0009
_MGMT_STATUS_SUCCESS = 0x00
_MGMT_SETTING_BONDABLE = 1 << 4

# Keep every diagnostic layer on the same visible marker without rewriting the
# already-tested v26 transport implementation.
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER


async def _read_bondable(mgmt, adapter_idx: int) -> bool:
    """Read the current controller Bondable bit without changing adapter state."""
    result = await mgmt._send_command_await(_MGMT_OP_READ_INFO, adapter_idx, b"")
    if result is None:
        raise BleakError(f"{_RAW_MARKER}: Mgmt Read Controller Information lieferte keine Antwort")
    status, data = result
    if status != _MGMT_STATUS_SUCCESS or len(data) < 17:
        raise BleakError(
            f"{_RAW_MARKER}: Mgmt Read Controller Information fehlgeschlagen "
            f"(status=0x{status:02x}, len={len(data)})"
        )
    # Read Controller Information response:
    # address[6], version[1], manufacturer[2], supported_settings[4],
    # current_settings[4], ...  => current_settings starts at byte 13.
    current_settings = int.from_bytes(data[13:17], "little")
    return bool(current_settings & _MGMT_SETTING_BONDABLE)


async def _set_bondable(mgmt, adapter_idx: int, enabled: bool) -> bool:
    """Set controller Bondable and report whether the mgmt command succeeded."""
    result = await mgmt._send_command_await(
        _MGMT_OP_SET_BONDABLE,
        adapter_idx,
        bytes([1 if enabled else 0]),
    )
    return result is not None and result[0] == _MGMT_STATUS_SUCCESS


async def _restore_bondable(mgmt, adapter_idx: int, enabled: bool) -> None:
    """Restore adapter Bondable state, retrying once because this is global state."""
    if await _set_bondable(mgmt, adapter_idx, enabled):
        return
    await asyncio.sleep(0.2)
    if await _set_bondable(mgmt, adapter_idx, enabled):
        return
    raise BleakError(
        f"{_RAW_MARKER}: ursprünglicher Bluetooth-Bondable-Zustand konnte nicht wiederhergestellt werden"
    )


async def _eqiva_passive_pair(self: EqivaRawATTClient, *args, **kwargs) -> None:
    """Mirror ESPHome with a temporary No-Bond Just-Works encrypted session."""
    mgmt, adapter_idx = self._require_mgmt()
    sock = self._sock
    if sock is None or not self._connected:
        raise BleakError("Eqiva raw ATT transport is not connected")

    loop = asyncio.get_running_loop()
    auth_failed: asyncio.Future[AuthenticationFailed] = loop.create_future()
    ltk_seen: asyncio.Future[NewLongTermKey] = loop.create_future()
    unsupported: asyncio.Future[str] = loop.create_future()
    reply_tasks: set[asyncio.Task[object]] = set()

    def _track(task: asyncio.Task[object]) -> None:
        reply_tasks.add(task)
        task.add_done_callback(reply_tasks.discard)

    def _capture(event) -> None:
        if isinstance(event, NewLongTermKey):
            self._trace_note(
                f"MGMT:new-ltk(store={event.store_hint},size={event.key.encryption_size})"
            )
            if not ltk_seen.done():
                ltk_seen.set_result(event)
            return

        if isinstance(event, UserConfirmationRequest):
            accept = bool(event.confirm_hint)
            self._trace_note(
                f"MGMT:user-confirm(hint={event.confirm_hint},accept={int(accept)})"
            )
            task = loop.create_task(
                mgmt.user_confirmation_reply(
                    adapter_idx,
                    self.address,
                    self._address_type,
                    accept=accept,
                )
            )
            _track(task)
            if not accept and not unsupported.done():
                unsupported.set_result("numeric-comparison")
            return

        if isinstance(event, UserPasskeyRequest):
            self._trace_note("MGMT:user-passkey-request")
            if not unsupported.done():
                unsupported.set_result("passkey-request")
            return

        if isinstance(event, AuthenticationFailed):
            self._trace_note(f"MGMT:auth-failed=0x{event.status:02x}")
            if not auth_failed.done():
                auth_failed.set_result(event)

    original_bondable = await _read_bondable(mgmt, adapter_idx)
    changed_bondable = False
    unregister = mgmt.register_pairing_handler(adapter_idx, self.address, _capture)
    settle = loop.create_task(asyncio.sleep(3.0))

    try:
        self._trace_note(f"MGMT:bondable-original={int(original_bondable)}")
        if original_bondable:
            if not await _set_bondable(mgmt, adapter_idx, False):
                raise BleakError(
                    f"{_RAW_MARKER}: Bluetooth-Adapter konnte für den Test nicht temporär auf No-Bond gesetzt werden"
                )
            changed_bondable = True
            self._trace_note("MGMT:bondable-temporary=0")

        before = sock.security_level
        if before < BT_SECURITY_MEDIUM:
            if not sock.set_security_level(BT_SECURITY_MEDIUM):
                raise BleakError(
                    f"Kernel konnte BT_SECURITY nicht von {before} auf {BT_SECURITY_MEDIUM} anheben"
                )
        self._trace_note(f"MGMT:no-bond-security={before}->{sock.security_level}")

        done, _ = await asyncio.wait(
            (settle, auth_failed, ltk_seen, unsupported),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if auth_failed in done:
            event = auth_failed.result()
            raise BleakError(
                f"{_RAW_MARKER}: No-Bond Just-Works-Authentifizierung fehlgeschlagen "
                f"(auth status 0x{event.status:02x})"
            )

        if unsupported in done:
            mode = unsupported.result()
            raise BleakError(
                f"{_RAW_MARKER}: Schloss fordert {mode}; Just Works wurde nicht angeboten"
            )

        if ltk_seen in done:
            event = ltk_seen.result()
            self._trace_note(
                f"MGMT:no-bond-ltk-ok(store={event.store_hint},size={event.key.encryption_size})"
            )
        else:
            self._trace_note("MGMT:no-bond-no-event-after-3s")

        if reply_tasks:
            await asyncio.gather(*tuple(reply_tasks))

        if self._sock is None or not self._connected:
            raise BleakError(
                f"{_RAW_MARKER}: BLE-Link wurde während der No-Bond-Security-Aushandlung getrennt"
            )
    finally:
        unregister()
        if not settle.done():
            settle.cancel()
        if changed_bondable:
            # This setting is adapter-global. Always restore the exact previous
            # state before returning or surfacing a pairing error.
            await asyncio.shield(_restore_bondable(mgmt, adapter_idx, original_bondable))
            self._trace_note(f"MGMT:bondable-restored={int(original_bondable)}")


EqivaRawATTClient.pair = _eqiva_passive_pair
