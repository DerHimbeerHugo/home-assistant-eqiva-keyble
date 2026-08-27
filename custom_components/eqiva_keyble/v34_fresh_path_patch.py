from __future__ import annotations

import logging

from homeassistant.components import bluetooth

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from . import v33_path_resilience_patch as _v33_patch
from .protocol import EqivaConnectionError, EqivaKeyBleClient, EqivaNotFoundError

_LOGGER = logging.getLogger(__name__)
_RAW_MARKER = "RAW-PDU-v34"

_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER

# v33 cached complete Home Assistant scanner-path objects. Those objects are
# intentionally short-lived and may no longer represent a usable raw L2CAP
# connection later. Restore the original live-path lookup and never reuse a
# cached BLEDevice/scanner path.
_LIVE_LOCAL_RAW_PATH = _v33_patch._ORIGINAL_LOCAL_RAW_PATH
_transport_patch._local_raw_path = _LIVE_LOCAL_RAW_PATH

# Bypass v33's cached-path connect wrapper entirely. This is the raw ATT connect
# implementation that already proved pairing/status/commands work when a current
# local path is available.
_BASE_CONNECT = _v33_patch._ORIGINAL_CONNECT


def _has_live_local_path(self: EqivaKeyBleClient) -> bool:
    return _LIVE_LOCAL_RAW_PATH(self) is not None


async def _request_fresh_path(self: EqivaKeyBleClient, reason: str) -> None:
    _LOGGER.debug(
        "Eqiva %s: %s requesting 7 second active scan for a fresh local hci path (%s)",
        self.address,
        _RAW_MARKER,
        reason,
    )
    await bluetooth.async_request_active_scan(self.hass, duration=7.0)

    if not _has_live_local_path(self):
        raise EqivaNotFoundError(
            f"{_RAW_MARKER}: Eqiva wurde gefunden, aber auch nach 7 Sekunden aktivem Scan "
            "steht kein aktueller lokaler hci-Bluetooth-Pfad für raw ATT zur Verfügung."
        )


async def _connect_v34(self: EqivaKeyBleClient) -> None:
    # A current scanner path is required for every new raw L2CAP connection.
    # Never feed a previously cached scanner/BLEDevice object back into Bleak.
    if not _has_live_local_path(self):
        await _request_fresh_path(self, "kein aktueller Scanner-Pfad")

    try:
        await _BASE_CONNECT(self)
        return
    except EqivaNotFoundError:
        # The short-lived path may have disappeared between the pre-check and
        # the actual connection attempt. Rescan once and retry from scratch.
        await _request_fresh_path(self, "Pfad während Verbindungsaufbau verschwunden")
        await _BASE_CONNECT(self)
        return
    except EqivaConnectionError as err:
        text = str(err)
        if "Errno 38" not in text and "Function not implemented" not in text:
            raise

        # ENOSYS after v33 is consistent with reusing a stale HA Bluetooth path.
        # Throw away that attempt, refresh discovery, and build a new Bleak/raw
        # client from the newly returned BLEDevice/scanner path.
        _LOGGER.warning(
            "Eqiva %s: %s raw L2CAP returned ENOSYS; refreshing Bluetooth path and retrying once",
            self.address,
            _RAW_MARKER,
        )
        await _request_fresh_path(self, "raw L2CAP ENOSYS")
        await _BASE_CONNECT(self)


EqivaKeyBleClient._connect = _connect_v34
