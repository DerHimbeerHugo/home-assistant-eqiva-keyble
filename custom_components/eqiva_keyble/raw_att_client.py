from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from habluetooth.channels.att import CCCD_UUID, ATTClient
from habluetooth.channels.l2cap import (
    BT_SECURITY_LOW,
    BT_SECURITY_MEDIUM,
    L2CAPSocket,
)
from habluetooth.client_mgmt import HaMgmtClient

_LOGGER = logging.getLogger(__name__)

_CCCD_NOTIFY = b"\x01\x00"
_CCCD_INDICATE = b"\x02\x00"
_CCCD_OFF = b"\x00\x00"
_ATT_WRITE_REQUEST = 0x12


class EqivaRawATTClient(HaMgmtClient):
    """Eqiva-specific raw L2CAP/ATT backend with safe ATT metadata tracing."""

    def _trace_pdu(self, direction: str, data: bytes) -> None:
        """Remember only ATT opcode/handle metadata; never retain payload bytes."""
        if not data:
            summary = f"{direction}:empty"
        else:
            opcode = data[0]
            summary = f"{direction}:0x{opcode:02x}"
            if opcode in (0x04, 0x0A, 0x0C, 0x12, 0x1B, 0x1D, 0x52) and len(data) >= 3:
                handle = int.from_bytes(data[1:3], "little")
                summary += f"@0x{handle:04x}"
            elif opcode == 0x01 and len(data) >= 5:
                req_opcode = data[1]
                handle = int.from_bytes(data[2:4], "little")
                error_code = data[4]
                summary += f"(req=0x{req_opcode:02x},handle=0x{handle:04x},err=0x{error_code:02x})"
            summary += f"[{len(data)}]"

        trace = getattr(self, "_eqiva_att_trace", None)
        if trace is None:
            trace = []
            self._eqiva_att_trace = trace
        trace.append(summary)
        del trace[:-12]

    def _trace_note(self, note: str) -> None:
        trace = getattr(self, "_eqiva_att_trace", None)
        if trace is None:
            trace = []
            self._eqiva_att_trace = trace
        trace.append(note)
        del trace[:-12]

    def trace_summary(self) -> str:
        trace = getattr(self, "_eqiva_att_trace", None) or []
        return " > ".join(trace) if trace else "keine ATT-PDUs aufgezeichnet"

    @property
    def requested_security_level(self) -> int | None:
        return self._sock.security_level if self._sock is not None else None

    async def _send_traced_pdu(self, data: bytes) -> None:
        self._trace_pdu("TX", data)
        await self._send_pdu(data)

    async def connect(self, pair: bool, **kwargs: Any) -> None:
        """Open raw ATT, skip MTU exchange, and discover GATT services."""
        if self._connected:
            raise BleakError("already connected")

        self._eqiva_att_trace: list[str] = []
        # Eqiva v25 deliberately disables ATTClient's synchronous security retry.
        # On ATT 0x05 habluetooth normally raises BT_SECURITY and immediately
        # re-issues the request. The kernel security procedure is asynchronous,
        # so that retry can race the LE encryption/authentication setup. We
        # instead raise link security explicitly after the KeyBLE nonce exchange.
        att = ATTClient(
            send=self._send_traced_pdu,
            on_disconnect=self._handle_disconnect,
            escalate_security=None,
        )
        self._att = att

        def _on_raw_data(data: bytes) -> None:
            self._trace_pdu("RX", data)
            att.data_received(data)

        _LOGGER.debug(
            "%s: opening Eqiva raw L2CAP/ATT connection with MTU fixed at 23",
            self.address,
        )

        try:
            with self._scanner.connecting():
                self._sock = await L2CAPSocket.create_connection(
                    source=self._adapter_address,
                    address=self.address,
                    address_type=self._address_type,
                    on_data=_on_raw_data,
                    on_close=att.connection_lost,
                    timeout=self._timeout,
                    security_level=BT_SECURITY_LOW,
                )
                services = await att.discover()
        except BaseException:
            self._handle_disconnect(None)
            raise

        self.services = self._build_services(services)
        self._connected = True

        if self._register_connection is not None:
            try:
                self._register_connection(self.address)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "%s: raw ATT connection slot register callback failed",
                    self.address,
                )

        _LOGGER.debug(
            "%s: Eqiva raw ATT connected; service discovery complete at MTU %s",
            self.address,
            self.mtu_size,
        )

    async def request_medium_link_security(self, settle_seconds: float = 2.0) -> None:
        """Request LE link security MEDIUM and allow the kernel security procedure to settle."""
        sock = self._sock
        if sock is None or not self._connected:
            raise BleakError("Eqiva raw ATT transport is not connected")

        before = sock.security_level
        if before < BT_SECURITY_MEDIUM:
            if not sock.set_security_level(BT_SECURITY_MEDIUM):
                raise BleakError(
                    f"Kernel konnte BT_SECURITY nicht von {before} auf {BT_SECURITY_MEDIUM} anheben"
                )
        self._trace_note(f"LINK:security={before}->{sock.security_level}")
        _LOGGER.debug(
            "%s: Eqiva requested L2CAP security level %s -> %s; settling %.1fs",
            self.address,
            before,
            sock.security_level,
            settle_seconds,
        )

        await asyncio.sleep(settle_seconds)
        if self._sock is None or not self._connected:
            raise BleakError(
                "Eqiva BLE-Link wurde während der BT_SECURITY_MEDIUM-Aushandlung getrennt"
            )
        self._trace_note(f"LINK:security-settled={self._sock.security_level}")

    def _notification_cccd(
        self, characteristic: BleakGATTCharacteristic
    ) -> tuple[BleakGATTDescriptor, bytes]:
        if "notify" in characteristic.properties:
            cccd_value = _CCCD_NOTIFY
        elif "indicate" in characteristic.properties:
            cccd_value = _CCCD_INDICATE
        else:
            raise BleakError("characteristic does not support notify or indicate")

        cccd = characteristic.get_descriptor(CCCD_UUID)
        if cccd is None:
            raise BleakError("characteristic has no client configuration descriptor")
        return cccd, cccd_value

    def prepare_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: Callable[[bytearray], None],
    ) -> None:
        """Install the v29 local handler before any CCCD write is attempted."""
        self._notification_cccd(characteristic)
        self._eqiva_seen_notification = False
        self._eqiva_notify_mode = "local-only-wait"

        def _observed_callback(data: bytearray) -> None:
            if not self._eqiva_seen_notification:
                self._eqiva_seen_notification = True
                if self._eqiva_notify_mode == "local-only-wait":
                    self._eqiva_notify_mode = "local-only-success"
                    self._trace_note("NOTIFY:rx-before-cccd-command")
            callback(data)

        descriptors = getattr(characteristic, "descriptors", None) or []
        descriptor_text = (
            ",".join(
                f"0x{descriptor.handle:04x}:{descriptor.uuid}"
                for descriptor in descriptors
            )
            or "none"
        )
        properties = ",".join(sorted(characteristic.properties))
        self._eqiva_gatt_profile = (
            f"rx_handle=0x{characteristic.handle:04x}; "
            f"props=[{properties}]; descriptors=[{descriptor_text}]"
        )
        self._trace_note(f"GATT:{self._eqiva_gatt_profile}")
        self._codec().set_notify_handler(characteristic.handle, _observed_callback)
        _LOGGER.debug(
            "%s: Eqiva raw ATT notify handler prepared locally for handle 0x%04x",
            self.address,
            characteristic.handle,
        )

    async def enable_prepared_notify(
        self, characteristic: BleakGATTCharacteristic
    ) -> None:
        """Keep v29 notification timing: local-only, then delayed CCCD command."""
        self._eqiva_notify_mode = "local-only-wait"
        self._trace_note("NOTIFY:local-only-window=250ms")

        async def _delayed_cccd_command() -> None:
            await asyncio.sleep(0.25)
            if self._eqiva_seen_notification:
                self._trace_note("NOTIFY:cccd-command-not-needed")
                return

            self._eqiva_notify_mode = "cccd-command-delayed"
            self._trace_note("NOTIFY:cccd-write-command-after-250ms")
            codec = self._codec()
            cccd, cccd_value = self._notification_cccd(characteristic)
            try:
                await codec.write_command(cccd.handle, cccd_value)
            except Exception as err:  # noqa: BLE001
                self._trace_note(
                    f"NOTIFY:cccd-command-error={type(err).__name__}:{err}"
                )
                return
            _LOGGER.debug(
                "%s: Eqiva raw ATT CCCD enabled via delayed Write Command "
                "(handle 0x%04x)",
                self.address,
                cccd.handle,
            )

        self._eqiva_v29_cccd_task = asyncio.create_task(_delayed_cccd_command())

    async def confirm_prepared_notify(
        self, characteristic: BleakGATTCharacteristic
    ) -> None:
        """Keep the proven v29 path without a protected CCCD Write Request."""
        self._trace_note("CCCD:write-request-skipped-v29")

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback,
        **kwargs: Any,
    ) -> None:
        self.prepare_notify(characteristic, callback)
        await self.enable_prepared_notify(characteristic)

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        codec = self._codec()
        cccd: BleakGATTDescriptor | None = characteristic.get_descriptor(CCCD_UUID)
        try:
            if cccd is not None and self._connected:
                await codec.write_command(cccd.handle, _CCCD_OFF)
        finally:
            codec.remove_notify_handler(characteristic.handle)

    async def write_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic,
        data,
        response: bool,
    ) -> None:
        """Send v37 real ATT Write Requests without awaiting ATT Write Response."""
        if not response:
            await super().write_gatt_char(characteristic, data, response)
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
        self._trace_note(f"WRITE:fire-and-forget-request@0x{characteristic.handle:04x}")
        await self._send_traced_pdu(payload)

    @property
    def notify_mode(self) -> str:
        return getattr(self, "_eqiva_notify_mode", "unknown")

    @property
    def gatt_profile(self) -> str:
        return getattr(self, "_eqiva_gatt_profile", "unknown")
