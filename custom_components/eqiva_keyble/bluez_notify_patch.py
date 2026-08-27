from __future__ import annotations

import asyncio
import logging

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import RECEIVE_CHARACTERISTIC_UUID, SEND_CHARACTERISTIC_UUID
from .protocol import (
    EqivaConnectionError,
    EqivaKeyBleClient,
    EqivaNotFoundError,
    EqivaProtocolError,
)

_LOGGER = logging.getLogger(__name__)


async def _eqiva_connect_fast_notify(self: EqivaKeyBleClient) -> None:
    """Eqiva-specific BlueZ notification setup.

    The lock is very timing-sensitive after service discovery. ESPHome's working
    implementation registers notifications and immediately continues with protocol
    initialization, so do not add an artificial idle delay here.
    """
    if self._client is not None and self._client.is_connected:
        return

    self._reset_session()
    self._reset_gatt()
    errors: list[str] = []

    for attempt in range(1, 3):
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

            # Force AcquireNotify on the first attempt. v0.1.9 tried to infer the
            # backend from backend_id, but Home Assistant's wrapper does not expose
            # 'bluez' there reliably, so that test accidentally used StartNotify.
            use_acquire_notify = attempt == 1
            notify_mode = "AcquireNotify" if use_acquire_notify else "StartNotify"
            stage = f"Notifications aktivieren ({notify_mode})"
            _LOGGER.debug(
                "Eqiva %s: enabling notifications immediately via %s",
                self.address,
                notify_mode,
            )
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
            _LOGGER.debug(
                "Eqiva %s: notifications enabled via %s",
                self.address,
                notify_mode,
            )
            return

        except EqivaProtocolError:
            await self._abort_connection()
            raise
        except Exception as err:  # noqa: BLE001
            errors.append(f"{stage}: {type(err).__name__}: {err}")
            await self._abort_connection()
            await self._clear_stale_connection()
            if attempt == 1:
                _LOGGER.warning(
                    "Eqiva %s: first notify/connect path failed (%s); retrying with StartNotify",
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


# Temporary compatibility patch while the Eqiva/BlueZ timing behavior is being
# validated on real hardware. Once stable, this will be folded back into protocol.py.
EqivaKeyBleClient._connect = _eqiva_connect_fast_notify
