from __future__ import annotations

import asyncio

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from .protocol import (
    EqivaKeyBleClient,
    EqivaProtocolError,
    MSG_ANSWER_WITHOUT_SECURITY,
    MSG_COMMAND,
    MSG_STATUS_CHANGED,
    MSG_STATUS_INFO,
)

_RAW_MARKER = "RAW-PDU-v31"

# v31 keeps the proven v29 transport exactly as-is. It changes the config-flow
# probe only: after the nonce handshake, send COMMAND=LOCK (0x87/0x00) as the
# first secure message instead of STATUS_REQUEST. This tells us whether the lock
# rejects every secure message or only STATUS_REQUEST.
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER


def _diag(self: EqivaKeyBleClient) -> str:
    mode, profile, trace = _v29_patch._backend_diag(self)
    return (
        f"notify_mode={mode}; GATT={profile}; ATT-Spur={trace}; "
        f"Secure-TX: {_secure_trace_patch._secure_tx_text(self)}"
    )


async def _status_v31_command_probe(self: EqivaKeyBleClient):
    """Probe secure KeyBLE with COMMAND=LOCK as the first secure message.

    This intentionally never returns a normal status. The config flow therefore
    cannot accidentally create an entry from the diagnostic build. A successful
    STATUS_CHANGED or STATUS_INFO is surfaced as an explicit diagnostic result.
    """
    async with self._operation_lock:
        saw_status_changed = False
        try:
            await self._connect()

            # CONNECTION_INFO is the last message from the nonce handshake.
            # Clear only the diagnostic message marker; keep the established
            # session, nonces and counters untouched.
            self._eqiva_last_rx_message_type = None
            self._eqiva_answer_without_security = None
            self._eqiva_status_info_ok = False

            await self._send_message(
                MSG_COMMAND,
                b"\x00",  # KeyBLE command 0 = LOCK
                secure=True,
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 8.0

            while loop.time() < deadline:
                message_type = getattr(self, "_eqiva_last_rx_message_type", None)

                if message_type == MSG_ANSWER_WITHOUT_SECURITY:
                    answer_code = getattr(self, "_eqiva_answer_without_security", None)
                    answer_text = (
                        f"0x{answer_code:02x}"
                        if isinstance(answer_code, int)
                        else "unbekannt"
                    )
                    raise EqivaProtocolError(
                        f"{_RAW_MARKER}: sicherer COMMAND LOCK (0x87/0x00) wurde ebenfalls "
                        f"mit ANSWER_WITHOUT_SECURITY abgelehnt (Antwortbyte={answer_text}). "
                        "Damit betrifft die Ablehnung nicht nur STATUS_REQUEST, sondern "
                        f"grundsätzlich sichere KeyBLE-Nachrichten dieser Session. {_diag(self)}"
                    )

                if message_type == MSG_STATUS_INFO and self.last_status is not None:
                    status = self.last_status
                    raise EqivaProtocolError(
                        f"{_RAW_MARKER}: ERFOLG: sicherer COMMAND LOCK (0x87/0x00) wurde vom "
                        "Schloss akzeptiert und mit sicherem STATUS_INFO (0x83) beantwortet. "
                        f"lock_status={status.lock_status}; battery_low={status.battery_low}; "
                        f"pairing_allowed={status.pairing_allowed}. Damit funktionieren User-Key, "
                        "Nonce-Session und sichere COMMAND-Nachrichten; der bisherige Fehler ist "
                        f"STATUS_REQUEST-spezifisch. {_diag(self)}"
                    )

                if message_type == MSG_STATUS_CHANGED:
                    saw_status_changed = True
                    # The ESPHome component treats 0x05 as a state-change signal
                    # and follows it with STATUS_REQUEST. We deliberately do not
                    # send that follow-up here, because it would mix both A/B paths.
                    self._eqiva_last_rx_message_type = None

                if self._client is None or not self._client.is_connected:
                    break

                await asyncio.sleep(0.05)

            if saw_status_changed:
                raise EqivaProtocolError(
                    f"{_RAW_MARKER}: ERFOLG: nach sicherem COMMAND LOCK (0x87/0x00) kam "
                    "STATUS_CHANGED (0x05). Das beweist, dass der sichere COMMAND vom Schloss "
                    "akzeptiert wurde. Es kam innerhalb des Diagnosefensters kein zusätzliches "
                    f"STATUS_INFO; absichtlich wurde kein STATUS_REQUEST nachgeschoben. {_diag(self)}"
                )

            last_rx = getattr(self, "_eqiva_last_rx_message_type", None)
            last_rx_text = (
                f"0x{last_rx:02x}" if isinstance(last_rx, int) else "none"
            )
            raise EqivaProtocolError(
                f"{_RAW_MARKER}: COMMAND LOCK wurde als erste sichere Nachricht gesendet, aber "
                "innerhalb von 8 Sekunden kam weder ANSWER_WITHOUT_SECURITY, STATUS_CHANGED noch "
                f"STATUS_INFO. last_rx={last_rx_text}. {_diag(self)}"
            )
        finally:
            await self._disconnect()


EqivaKeyBleClient.status = _status_v31_command_probe
