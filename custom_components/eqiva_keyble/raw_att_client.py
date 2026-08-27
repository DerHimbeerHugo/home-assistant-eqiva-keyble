from __future__ import annotations

import logging
from typing import Any, Callable

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


class EqivaRawATTClient(HaMgmtClient):
    """Eqiva-specific raw L2CAP/ATT backend.

    Eqiva Key-BLE locks are known to misbehave when the central performs an ATT
    Exchange MTU during connection setup. This backend deliberately leaves ATT at
    the Bluetooth default MTU of 23 and talks to the lock directly over the kernel
    L2CAP ATT fixed channel.

    The lock also disconnects when its CCCD is written as a normal ATT Write
    Request on this raw path. Therefore the notification handler is registered
    locally and the CCCD is enabled with a non-blocking ATT Write Command. Key-BLE
    characteristic traffic can still use the protocol-correct Write Request with
    response independently.
    """

    async def connect(self, pair: bool, **kwargs: Any) -> None:
        """Open raw ATT, skip MTU exchange, and discover GATT services."""
        if self._connected:
            raise BleakError("already connected")

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

                # IMPORTANT: never call att.exchange_mtu() for this lock.
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

    def _notification_cccd(
        self, characteristic: BleakGATTCharacteristic
    ) -> tuple[BleakGATTDescriptor, bytes]:
        """Resolve the CCCD and value for a notify/indicate characteristic."""
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
        """Register the notification handler locally without touching the CCCD."""
        self._notification_cccd(characteristic)  # validate before registering
        self._codec().set_notify_handler(characteristic.handle, callback)
        _LOGGER.debug(
            "%s: Eqiva raw ATT notify handler prepared locally for handle 0x%04x",
            self.address,
            characteristic.handle,
        )

    async def enable_prepared_notify(
        self, characteristic: BleakGATTCharacteristic
    ) -> None:
        """Enable a prepared notification with an ATT Write Command."""
        codec = self._codec()
        cccd, cccd_value = self._notification_cccd(characteristic)
        try:
            # Eqiva drops the raw ATT link when this descriptor is sent as a
            # Write Request. A Write Command does not create an ATT transaction
            # and therefore lets the Key-BLE CONNECTION_REQUEST follow directly.
            await codec.write_command(cccd.handle, cccd_value)
        except BaseException:
            codec.remove_notify_handler(characteristic.handle)
            raise

        _LOGGER.debug(
            "%s: Eqiva raw ATT CCCD enabled via Write Command (handle 0x%04x)",
            self.address,
            cccd.handle,
        )

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback,
        **kwargs: Any,
    ) -> None:
        """Standard combined helper retained for Bleak compatibility."""
        self.prepare_notify(characteristic, callback)
        await self.enable_prepared_notify(characteristic)

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Disable notifications without starting an ATT request transaction."""
        codec = self._codec()
        cccd: BleakGATTDescriptor | None = characteristic.get_descriptor(CCCD_UUID)
        try:
            if cccd is not None and self._connected:
                await codec.write_command(cccd.handle, _CCCD_OFF)
        finally:
            codec.remove_notify_handler(characteristic.handle)
