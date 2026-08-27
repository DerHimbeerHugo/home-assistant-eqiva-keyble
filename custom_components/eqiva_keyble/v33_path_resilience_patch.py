from __future__ import annotations

import logging

from homeassistant.components import bluetooth

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch
from .protocol import EqivaKeyBleClient, EqivaNotFoundError

_LOGGER = logging.getLogger(__name__)
_RAW_MARKER = "RAW-PDU-v33"

_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER

_ORIGINAL_LOCAL_RAW_PATH = _transport_patch._local_raw_path
_ORIGINAL_CONNECT = EqivaKeyBleClient._connect

# Home Assistant's current scanner-path list is intentionally short lived.  A
# lock may therefore disappear from async_scanner_devices_by_address() between
# two commands even though the local adapter is still usable.  Keep the last
# proven local hci path for the lifetime of Home Assistant and use it as a
# fallback.
_RAW_PATH_CACHE: dict[str, object] = {}


def _local_raw_path_v33(self: EqivaKeyBleClient):
    path = _ORIGINAL_LOCAL_RAW_PATH(self)
    if path is not None:
        _RAW_PATH_CACHE[self.address] = path
        return path

    cached = _RAW_PATH_CACHE.get(self.address)
    if cached is not None:
        _LOGGER.debug(
            "Eqiva %s: %s using cached local hci raw path because the current "
            "Home Assistant scanner-path list is temporarily empty",
            self.address,
            _RAW_MARKER,
        )
        return cached
    return None


_transport_patch._local_raw_path = _local_raw_path_v33


async def _connect_v33(self: EqivaKeyBleClient) -> None:
    try:
        await _ORIGINAL_CONNECT(self)
        return
    except EqivaNotFoundError as err:
        # Only recover the specific transient path-cache failure.  Other
        # discovery/not-found conditions retain their original error semantics.
        if "kein lokaler hci-Bluetooth-Adapter" not in str(err):
            raise

        _LOGGER.debug(
            "Eqiva %s: %s no current local hci raw path; requesting active "
            "Bluetooth scan before retry",
            self.address,
            _RAW_MARKER,
        )
        await bluetooth.async_request_active_scan(self.hass, duration=5.0)

        # The active scan may have repopulated the current scanner path.  If not,
        # _local_raw_path_v33 can still fall back to the last proven path.
        await _ORIGINAL_CONNECT(self)


EqivaKeyBleClient._connect = _connect_v33
