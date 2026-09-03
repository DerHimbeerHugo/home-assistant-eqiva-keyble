# Eqiva KeyBLE transport architecture

This document describes the v0.4 transport architecture. The Home Assistant
GATT path has now been confirmed on real hardware with an ESPHome Bluetooth
Proxy, including status reads, live notifications and motor commands.

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
runtime behavior from the former experimental patch chain:

- a current local `hci` scanner path is required;
- static advertisement history is cleared and the next genuinely new local
  advertisement is used as the wake/connect signal;
- raw L2CAP/ATT service discovery deliberately keeps MTU 23;
- the notification handler is installed locally before the KeyBLE connection
  request; after 250 ms without a notification, the CCCD is sent as the known
  non-blocking Write Command;
- the later protected CCCD Write Request / SMP experiment stays disabled;
- normal KeyBLE writes remain true ATT Write Requests (`0x12`), but no ATT
  transaction future waits for `0x13`.

The raw path remains the local reference implementation. Its externally visible
behavior must stay unchanged while v0.4 cleanup continues.

## Effective patch chain before the refactor

Before the transport refactor, `__init__.py` imported runtime layers in this
order:

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

Those development modules were removed from the v0.4 working tree after their
effective behavior had been consolidated into the transport and protocol
modules. Their complete source and evolution remain available in Git history.

## Home Assistant GATT transport

`ha_gatt_transport.py` asks Home Assistant for a connectable `BLEDevice` and
uses `bleak-retry-connector.establish_connection`. It does not filter for
`hciX`, so Home Assistant can provide either a local adapter or an ESPHome
Bluetooth Proxy backend. It also waits for a fresh advertisement from any
connectable Home Assistant scanner path.

### ESPHome notification behavior

A normal bleak-esphome notification setup registers the proxy callback and then
writes the characteristic's CCCD. On the tested Eqiva lock that protected CCCD
write is rejected before the KeyBLE nonce exchange with ATT error `0x05`
(insufficient authentication).

The proven Raw-ATT path does not require that protected descriptor write.
Therefore the ESPHome path registers bleak-esphome's proxy-side notification
callback directly and deliberately skips the CCCD write. This mirrors the
working Eqiva notification behavior without attempting BLE bonding or exposing
KeyBLE credentials.

### Write Request behavior

HA GATT uses `response=True` for KeyBLE writes because `response=False` would
emit an ATT Write Command and is not equivalent to the Raw-ATT behavior. The
normal Bleak/proxy call may also wait for the ATT Write Response.

This difference was a hardware-test risk during initial v0.4 development. On
the tested ESPHome Bluetooth Proxy setup, status traffic and lock/unlock motor
commands complete successfully, so the HA-GATT write path is now considered
hardware-confirmed. A motor command is still never retried after it has been
sent.

## Transport selection

| Requested value | Resolution |
|---|---|
| `raw_att` | Always Raw ATT; requires a usable local Linux/hci path |
| `ha_gatt` | Always HA GATT; never falls back to Raw ATT |

Selection happens before connecting. A connection error never changes the
selected transport. The selected transport is included in the debug log.

Existing config entries have no transport option. They resolve to `raw_att`, so
no config-entry migration is required for the current explicit-selection beta.
Automatic selection can be added later without changing the command safety
boundary.

## Historical patch inventory

The following files existed during protocol and Bluetooth reverse engineering.
They are no longer shipped in the v0.4 working tree; their relevant behavior is
now represented by normal runtime modules.

| Historical file | Previous role | v0.4 replacement/status |
|---|---|---|
| `bluez_notify_patch.py` | Raw connect, GATT lookup, nonce/link-security sequencing | Consolidated into Raw transport and protocol session |
| `secure_trace_patch.py` | Secure-frame diagnosis | Removed; no runtime dependency |
| `v29_diagnostic_patch.py` | Local notify handler, delayed CCCD command, skip protected CCCD request | Consolidated into `raw_att_client.py` |
| `v30_esp_year_patch.py` | Inactive ESPHome year-byte experiment | Removed; was already not imported |
| `v31_command_probe.py` | Inactive destructive command probe | Removed; was already not imported |
| `v32_pairing_probe.py` | Marker for fresh Key Card pairing | Removed; no executable runtime behavior |
| `v33_path_resilience_patch.py` | Cached scanner path experiment | Removed; superseded during development |
| `v34_fresh_path_patch.py` | Fresh-path/ENOSYS retry | Removed; superseded during development |
| `v35_advertisement_connect_patch.py` | Fresh local advertisement loop | Removed; superseded during development |
| `v36_static_advertisement_wake_patch.py` | Static-history clear and fresh local wake | Consolidated into `raw_att_transport.py` |
| `v37_fire_and_forget_write_patch.py` | Real Write Request without waiting for response | Consolidated into `raw_att_client.py` |
| `passive_security_patch.py` | No-bond link-security experiment | Removed; was already not imported |

## Command safety boundary

Connection and KeyBLE nonce-session establishment may retry before a motor
command. Once the retrying or live client starts writing `COMMAND`, its retry
loop has already ended. A later timeout or write error therefore never causes a
second lock/unlock/open command. This rule is transport-independent and has a
hardware-free regression test.
