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

_RAW_MARKER = "RAW-PDU-v27"

# Keep every diagnostic layer on the same visible marker without rewriting the
# already-tested v26 transport implementation.
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER


async def _eqiva_passive_pair(self: EqivaRawATTClient, *args, **kwargs) -> None:
    """Mirror ESPHome: accept peer-driven Just-Works security on the live link."""
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

    unregister = mgmt.register_pairing_handler(adapter_idx, self.address, _capture)
    settle = loop.create_task(asyncio.sleep(3.0))
    try:
        before = sock.security_level
        if before < BT_SECURITY_MEDIUM:
            if not sock.set_security_level(BT_SECURITY_MEDIUM):
                raise BleakError(
                    f"Kernel konnte BT_SECURITY nicht von {before} auf {BT_SECURITY_MEDIUM} anheben"
                )
        self._trace_note(f"MGMT:passive-security={before}->{sock.security_level}")

        done, _ = await asyncio.wait(
            (settle, auth_failed, ltk_seen, unsupported),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if auth_failed in done:
            event = auth_failed.result()
            raise BleakError(
                f"{_RAW_MARKER}: passive BLE-Authentifizierung fehlgeschlagen "
                f"(auth status 0x{event.status:02x})"
            )

        if unsupported in done:
            mode = unsupported.result()
            raise BleakError(
                f"{_RAW_MARKER}: Schloss fordert {mode}; Just Works wurde nicht angeboten"
            )

        if ltk_seen in done:
            self._trace_note("MGMT:passive-security-ltk-ok")
        else:
            self._trace_note("MGMT:passive-security-no-event-after-3s")

        if reply_tasks:
            await asyncio.gather(*tuple(reply_tasks))

        if self._sock is None or not self._connected:
            raise BleakError(
                f"{_RAW_MARKER}: BLE-Link wurde während der passiven Security-Aushandlung getrennt"
            )
    finally:
        unregister()
        if not settle.done():
            settle.cancel()


EqivaRawATTClient.pair = _eqiva_passive_pair
