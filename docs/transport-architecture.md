# Eqiva KeyBLE transport architecture

This document describes the experimental v0.4 development architecture. It
does not claim that an ESPHome Bluetooth Proxy works with this lock; that path
still requires a real-hardware test.

## Runtime layers

```text
Home Assistant entities / coordinator
              |
      Eqiva client / session
              |
       KeyBLE protocol.py
              |
       EqivaTransport API
          /          \
   RawAttTransport   HomeAssistantGattTransport
     local hci       HA-selected local/proxy path
```

`protocol.py` owns KeyBLE framing, fragmentation, AES authentication and
encryption, nonce/session state, status parsing, pairing, and the command
no-retry boundary. It has no Bleak characteristic or BlueZ socket logic.

`transport.py` contains only the operations KeyBLE needs: connect (including
service and notification setup), fragment write, post-nonce transport hook,
disconnect and connection state.

## Raw ATT reference

`raw_att_transport.py` and `raw_att_client.py` contain the effective proven
runtime behavior from the experimental patch chain:

- a current local `hci` scanner path is required;
- static advertisement history is cleared and the next genuinely new local
  advertisement is used as the wake/connect signal (v36);
- raw L2CAP/ATT service discovery deliberately keeps MTU 23;
- the notification handler is installed locally before the KeyBLE connection
  request; after 250 ms without a notification, the CCCD is sent as the known
  non-blocking Write Command (v29);
- the later protected CCCD Write Request / SMP experiment stays disabled
  (effective v29 behavior);
- normal KeyBLE writes remain true ATT Write Requests (`0x12`), but no ATT
  transaction future waits for `0x13` (v37).

The raw path is the default for existing entries and the known-working
reference. Its behavior must be hardware-regression-tested before any further
cleanup.

## Effective patch chain before the refactor

`__init__.py` previously imported the runtime layers in this order:

`bluez_notify_patch` → `secure_trace_patch` → v29 → v32 → v33 → v34 → v35 →
v36 → v37.

The final effective call sites were:

- `EqivaKeyBleClient._connect`: v36, calling the original raw connect retained
  through the v33–v35 base-function references;
- `_on_disconnect` and `_ensure_nonces_exchanged`: `bluez_notify_patch`;
- `request_status`: v29 around the original function retained by
  `secure_trace_patch`;
- Raw notify preparation and CCCD behavior: v29;
- Raw characteristic writes: v37;
- v32: marker changes only, with no protocol-byte change.

`v30_esp_year_patch.py`, `v31_command_probe.py`, and
`passive_security_patch.py` were not imported by the runtime entry point.

## Home Assistant GATT experiment

`ha_gatt_transport.py` asks Home Assistant for a connectable `BLEDevice` and
uses `bleak-retry-connector.establish_connection`. It does not filter for
`hciX`, so Home Assistant can provide either a local adapter or an ESPHome
Bluetooth Proxy backend. It also waits for a fresh advertisement from any
connectable Home Assistant scanner path.

The unresolved v37 difference is intentional and visible in code:

- `response=True` requests a real ATT Write Request, but the normal Bleak/proxy
  call also waits for its ATT Write Response;
- `response=False` would generate an ATT Write Command and is never used as a
  substitute;
- if the lock's KeyBLE notification wins the race and the backend still times
  out waiting for the ATT response, that failure is expected to appear during
  the hardware test.

## Transport selection

| Requested value | Resolution |
|---|---|
| `raw_att` | Always Raw ATT; requires a usable local Linux/hci path |
| `ha_gatt` | Always HA GATT; never falls back to Raw ATT |

Selection happens before connecting. A connection error never changes the
selected transport. The selected transport is included in the debug log.

Existing config entries have no transport option. They resolve to `raw_att`, so
no config-entry migration is required.

## Historical patch inventory

The patch files remain in the repository as development history, but
`__init__.py` no longer imports any of them and no runtime monkey-patching is
performed.

| File | Previous role | v0.4 runtime status |
|---|---|---|
| `bluez_notify_patch.py` | Raw connect, GATT lookup, nonce/link-security sequencing | Consolidated into Raw transport and protocol session |
| `secure_trace_patch.py` | Secure-frame diagnosis | Historical only; no longer imported |
| `v29_diagnostic_patch.py` | Local notify handler, delayed CCCD command, skip protected CCCD request | Consolidated into `raw_att_client.py` |
| `v30_esp_year_patch.py` | Inactive ESPHome year-byte experiment | Historical only; was already not imported |
| `v31_command_probe.py` | Inactive destructive command probe | Historical only; was already not imported |
| `v32_pairing_probe.py` | Marker for fresh Key Card pairing | Historical only; no executable behavior |
| `v33_path_resilience_patch.py` | Cached scanner path experiment | Historical; later superseded by v34 |
| `v34_fresh_path_patch.py` | Fresh-path/ENOSYS retry | Historical; later superseded by v35 |
| `v35_advertisement_connect_patch.py` | Fresh local advertisement loop | Historical; later superseded by v36 |
| `v36_static_advertisement_wake_patch.py` | Static-history clear and fresh local wake | Consolidated into `raw_att_transport.py` |
| `v37_fire_and_forget_write_patch.py` | Real Write Request without waiting for response | Consolidated into `raw_att_client.py` |
| `passive_security_patch.py` | No-bond link-security experiment | Historical only; was already not imported |

## Command safety boundary

Connection and KeyBLE nonce-session establishment may retry before a motor
command. Once the retrying or live client starts writing `COMMAND`, its retry
loop has already ended. A later timeout or write error therefore never causes a
second lock/unlock/open command. This existing rule remains
transport-independent and has a hardware-free regression test.
