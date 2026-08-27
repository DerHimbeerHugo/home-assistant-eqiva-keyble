from __future__ import annotations

import logging
from typing import Any

from bleak import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from habluetooth.channels.att import ATTClient, CCCD_UUID
from habluetooth.channels.l2cap import BT_SECURITY_LOW, L2CAPSocket
from habluetooth.client_mgmt import HaMgmtClient

_LOGGER = logging.getLogger(__name__)

_CCCD_NOTIFY = b"\x01\x00"
_CCCD_INDICATE = b"\x02\x00"
_CCCD_OFF = b"\x00\x00"
_ATT_WRITE_COMMAND = 0x52


class EqivaRawATTClient(HaMgmtClient):
    """Eqiva-specific raw L2CAP/ATT backend.

    Eqiva Key-BLE locks are known to misbehave when the central performs an ATT
    Exchange MTU during connection setup. The experimental habluetooth mgmt
    backend normally performs that exchange before GATT discovery. This backend
    deliberately leaves ATT at the Bluetooth default MTU of 23 and talks to the
    lock directly over the kernel L2CAP ATT fixed channel.

    Eqiva also does not reliably answer ATT write requests. Characteristic data
    writes are therefore issued by the protocol layer as Write Commands. For
    notification setup we go one step further and emit the CCCD Write Command
    PDU directly on the raw ATT socket so no Bleak/habluetooth request helper can
    accidentally wait for or translate a response.
    """

    async def connect(self, pair: bool, **kwargs: Any) -> None:
        """Open raw ATT, skip MTU exchange, and discover GATT services."""
        if self._connected:
            raise BleakError("already connected")

        # Eqiva uses application-level Key-BLE authentication, not Bluetooth
        # pairing/bonding, so the raw link intentionally starts at LOW security.
        att = ATTClient(
            send=self._send_pdu,
            on_disconnect=self._handle_disconnect,
            escalate_security=self._escalate_security,
        )
        self._att = att

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
                    on_data=att.data_received,
                    on_close=att.connection_lost,
                    timeout=self._timeout,
                    security_level=BT_SECURITY_LOW,
                )

                # IMPORTANT: do not call att.exchange_mtu(). Eqiva works with
                # the default ATT MTU (23) but can become unusable after a normal
                # Exchange MTU request.
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

    async def _raw_write_command(self, handle: int, value: bytes) -> None:
        """Emit one ATT Write Command PDU directly on the L2CAP channel."""
        if self._sock is None or not self._connected:
            raise BleakError("raw ATT socket is not connected")
        if len(value) > 20:  # MTU 23 minus opcode+handle
            raise BleakError("raw ATT Write Command payload exceeds MTU 23")
        pdu = bytes([_ATT_WRITE_COMMAND]) + handle.to_bytes(2, "little") + value
        _LOGGER.debug(
            "%s: raw ATT TX Write Command handle=0x%04x len=%d",
            self.address,
            handle,
            len(value),
        )
        await self._sock.send(pdu)

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback,
        **kwargs: Any,
    ) -> None:
        """Enable notifications with a directly emitted CCCD Write Command."""
        codec = self._codec()
        if "notify" in characteristic.properties:
            cccd_value = _CCCD_NOTIFY
        elif "indicate" in characteristic.properties:
            cccd_value = _CCCD_INDICATE
        else:
            raise BleakError("characteristic does not support notify or indicate")

        cccd = characteristic.get_descriptor(CCCD_UUID)
        if cccd is None:
            raise BleakError("characteristic has no client configuration descriptor")

        # Register locally first so a fast CONNECTION_INFO notification cannot
        # race past us. Then emit opcode 0x52 directly; a Write Command has no
        # ATT transaction/future and cannot legitimately produce a Write Response.
        codec.set_notify_handler(characteristic.handle, callback)
        try:
            await self._raw_write_command(cccd.handle, cccd_value)
        except BaseException:
            codec.remove_notify_handler(characteristic.handle)
            raise

        _LOGGER.debug(
            "%s: Eqiva raw ATT notifications primed via direct CCCD Write Command "
            "(handle 0x%04x)",
            self.address,
            cccd.handle,
        )

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Disable notifications without waiting for an ATT response."""
        codec = self._codec()
        cccd: BleakGATTDescriptor | None = characteristic.get_descriptor(CCCD_UUID)
        try:
            if cccd is not None and self._sock is not None and self._connected:
                await self._raw_write_command(cccd.handle, _CCCD_OFF)
        finally:
            codec.remove_notify_handler(characteristic.handle)
