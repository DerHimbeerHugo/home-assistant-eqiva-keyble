from __future__ import annotations

import asyncio
import logging
import os

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID
from .protocol import (
    EqivaConnectionError,
    EqivaHandshakeError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
    MSG_CONNECTION_INFO,
    MSG_CONNECTION_REQUEST,
)

_LOGGER = logging.getLogger(__name__)


def _register_bluez_callback_early(self: EqivaKeyBleClient) -> bool:
    """Register the receive callback locally without enabling the CCCD yet."""
    client = self._client
    characteristic = self._receive_characteristic
    if client is None or characteristic is None:
        return False

    backend = getattr(client, "_backend", None)
    callbacks = getattr(backend, "_notification_callbacks", None)
    obj = getattr(characteristic, "obj", None)
    if not isinstance(callbacks, dict) or not obj:
        return False

    try:
        char_path = obj[0]
    except (TypeError, IndexError):
        return False
    if not isinstance(char_path, str):
        return False

    def _backend_callback(data: bytearray) -> None:
        current = self._receive_characteristic
        if current is not None:
            self._notification_callback(current, data)

    callbacks[char_path] = _backend_callback
    _LOGGER.debug(
        "Eqiva %s: BlueZ receive callback pre-registered before CCCD enable",
        self.address,
    )
    return True


async def _eqiva_connect_primed_notify(self: EqivaKeyBleClient) -> None:
    """Connect using the ordering used by the working ESPHome implementation.

    ESPHome registers receive handling locally, immediately sends the initial
    CONNECTION_REQUEST and only then completes remote CCCD activation. BlueZ
    normally tries to complete CCCD activation before returning from start_notify(),
    which this lock rejects with ATT 0x0E. Prime the Key-BLE session first.
    """
    if self._client is not None and self._client.is_connected:
        return

    self._reset_session()
    self._reset_gatt()
    errors: list[str] = []

    for attempt in range(1, 3):
        waiter = None
        await self._clear_stale_connection()
        if attempt > 1:
            await asyncio.sleep(1.0)

        device = self._fresh_ble_device()
        if device is None:
            raise EqivaNotFoundError(
                f"{self.address} wurde von Home Assistant Bluetooth noch nicht gefunden"
            )

        stage = f"BLE-Verbindung / GATT-Serviceauflösung (Versuch {attempt}/2)"
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback=self._on_disconnect,
                max_attempts=1,
                use_services_cache=attempt == 1,
            )

            stage = "GATT-Characteristics auflösen"
            services = self._client.services
            self._send_characteristic = services.get_characteristic(
                SEND_CHARACTERISTIC_UUID
            )
            self._receive_characteristic = services.get_characteristic(
                RECEIVE_CHARACTERISTIC_UUID
            )
            if self._send_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic wurde im GATT-Profil nicht gefunden"
                )
            if self._receive_characteristic is None:
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic wurde im GATT-Profil nicht gefunden"
                )

            send_properties = set(self._send_characteristic.properties)
            receive_properties = set(self._receive_characteristic.properties)
            if "write" in send_properties:
                self._write_with_response = True
            elif "write-without-response" in send_properties:
                self._write_with_response = False
            else:
                raise EqivaConnectionError(
                    "Eqiva Send-Characteristic ist nicht beschreibbar: "
                    f"{sorted(send_properties)}"
                )

            if not ({"notify", "indicate"} & receive_properties):
                raise EqivaConnectionError(
                    "Eqiva Receive-Characteristic unterstützt keine Notifications: "
                    f"{sorted(receive_properties)}"
                )

            # Mirror the ESPHome ordering: local receive handling first, then
            # CONNECTION_REQUEST before the remote CCCD is enabled.
            _register_bluez_callback_early(self)
            self._local_nonce = os.urandom(8)
            waiter = self._new_waiter(MSG_CONNECTION_INFO)

            stage = "CONNECTION_REQUEST vor Notify-Aktivierung senden"
            _LOGGER.debug(
                "Eqiva %s: priming Key-BLE session with CONNECTION_REQUEST before CCCD",
                self.address,
            )
            await self._send_message(
                MSG_CONNECTION_REQUEST,
                bytes([self.user_id]) + self._local_nonce,
                secure=False,
            )

            # StartNotify is closest to ESPHome's explicit CCCD write. Keep
            # AcquireNotify as a second clean-connection fallback.
            use_acquire_notify = attempt == 2
            notify_mode = "AcquireNotify" if use_acquire_notify else "StartNotify"
            stage = f"Notifications aktivieren ({notify_mode}) nach CONNECTION_REQUEST"
            kwargs = (
                {"bluez": {"use_start_notify": False}}
                if use_acquire_notify
                else {}
            )
            await self._client.start_notify(
                self._receive_characteristic,
                self._notification_callback,
                **kwargs,
            )

            stage = "CONNECTION_INFO nach Notify-Aktivierung empfangen"
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=1.0)
            except asyncio.TimeoutError:
                # The priming response may have been emitted before the CCCD became
                # active. Repeat the same request once now that notifications are on.
                _LOGGER.debug(
                    "Eqiva %s: no CONNECTION_INFO after priming; repeating CONNECTION_REQUEST",
                    self.address,
                )
                await self._send_message(
                    MSG_CONNECTION_REQUEST,
                    bytes([self.user_id]) + self._local_nonce,
                    secure=False,
                )
                await asyncio.wait_for(waiter, timeout=4.0)

            if self._remote_nonce is None:
                raise EqivaHandshakeError(
                    "Notify ist aktiv, aber das Schloss hat keine CONNECTION_INFO-Nonce geliefert"
                )

            _LOGGER.debug(
                "Eqiva %s: notifications and nonce handshake established via %s",
                self.address,
                notify_mode,
            )
            return

        except EqivaProtocolError:
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            await self._abort_connection()
            raise
        except Exception as err:  # noqa: BLE001
            if waiter is not None:
                self._cancel_waiter(MSG_CONNECTION_INFO, waiter)
            errors.append(f"{stage}: {type(err).__name__}: {err}")
            await self._abort_connection()
            await self._clear_stale_connection()
            if attempt == 1:
                _LOGGER.warning(
                    "Eqiva %s: primed StartNotify path failed (%s); retrying with AcquireNotify",
                    self.address,
                    errors[-1],
                )
                continue
            first = errors[0] if errors else "unbekannt"
            raise EqivaConnectionError(
                f"{stage} fehlgeschlagen ({type(err).__name__}: {err}); "
                f"Versuch 1 war: {first}"
            ) from err

    raise EqivaConnectionError(
        "BLE-/GATT-Verbindungsaufbau nach zwei Versuchen fehlgeschlagen: "
        + " | ".join(errors)
    )


async def _eqiva_ensure_nonces_exchanged(self: EqivaKeyBleClient) -> None:
    """The patched connect path already performs the nonce exchange."""
    if self._remote_nonce is not None and self._local_nonce is not None:
        return
    await self._connect()
    if self._remote_nonce is None or self._local_nonce is None:
        raise EqivaHandshakeError(
            "Bluetooth-Verbindung steht, aber der KeyBLE-Nonce-Handshake wurde nicht abgeschlossen"
        )


# Temporary compatibility patch while Eqiva/BlueZ behavior is validated on real
# hardware. Once stable, fold this back into protocol.py.
EqivaKeyBleClient._connect = _eqiva_connect_primed_notify
EqivaKeyBleClient._ensure_nonces_exchanged = _eqiva_ensure_nonces_exchanged
