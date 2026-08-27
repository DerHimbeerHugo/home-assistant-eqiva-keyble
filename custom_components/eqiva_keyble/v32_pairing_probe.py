from __future__ import annotations

from . import bluez_notify_patch as _transport_patch
from . import secure_trace_patch as _secure_trace_patch
from . import v29_diagnostic_patch as _v29_patch

_RAW_MARKER = "RAW-PDU-v32"

# v32 deliberately stops changing protocol bytes.  It keeps the proven v29
# raw ATT transport and uses the existing Key-Card pairing flow to register a
# fresh KeyBLE user directly from Home Assistant.  This removes inherited/stale
# user credentials from the diagnostic equation.
_transport_patch._RAW_MARKER = _RAW_MARKER
_secure_trace_patch._RAW_MARKER = _RAW_MARKER
_v29_patch._RAW_MARKER = _RAW_MARKER
