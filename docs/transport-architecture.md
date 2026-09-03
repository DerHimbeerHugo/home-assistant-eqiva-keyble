# Eqiva Key-BLE Bluetooth architecture

## Runtime architecture

```text
Home Assistant
    │
Eqiva Coordinator
    │
KeyBLE session / protocol
    │
internal backend selection (not user-configurable)
    │
    ├── local Linux/BlueZ hci path ──> RawAttTransport
    │                                  │
    │                                  └── raw L2CAP/ATT, MTU 23
    │
    └── ESPHome / non-local HA path ─> HomeAssistantGattTransport
                                       │
                                       └── Home Assistant Bluetooth / Bleak
```

The KeyBLE protocol owns framing, AES authentication/encryption, nonces, counters, pairing, status requests and motor commands. Home Assistant supplies Bluetooth discovery and the current connectable scanner paths. The user never chooses a transport.

## Why two internal backends remain

The v0.4b4 hardware test showed that the Eqiva lock does not reliably complete the normal local BlueZ notification path. The connection reached the local BlueZ/Bleak backend, but notification activation produced GATT `0x0E` and `org.bluez.Error.NotConnected` errors.

BlueZ intentionally owns CCCD subscription for remote GATT characteristics through `StartNotify` / `AcquireNotify` and blocks applications from writing the CCCD directly through the normal descriptor D-Bus API. The proven Eqiva local behavior, however, requires a locally installed notification handler followed by a delayed CCCD **ATT Write Command** if no notification arrives first.

Because the normal BlueZ GATT API cannot express that wire behavior, v0.4 keeps a narrowly scoped local Raw ATT compatibility backend. The old development patch chain remains removed; only the consolidated `raw_att_client.py` and `raw_att_transport.py` implementation is retained.

## Automatic internal selection

There is no transport selector in the config flow or options flow.

Before a KeyBLE client is created, the integration checks Home Assistant's current connectable paths for the lock:

- when a usable local Linux `hci` path is present, `RawAttTransport` is selected;
- otherwise `HomeAssistantGattTransport` is selected, allowing an ESPHome Bluetooth Proxy or another non-local Home Assistant path to provide the connection.

The selection happens before session establishment. A motor command is never replayed on another backend after it has been sent.

## Local Raw ATT compatibility path

The local backend preserves the hardware-confirmed Eqiva wire behavior:

1. wait for a fresh advertisement from the local `hci` path;
2. open a raw L2CAP/ATT connection with MTU fixed at 23;
3. discover the GATT profile;
4. install the notification handler locally;
5. allow a 250 ms local-only notification window;
6. if no notification arrives, send the CCCD value as an ATT Write Command;
7. send KeyBLE fragments as real ATT Write Requests without waiting for an ATT Write Response transaction;
8. complete the normal KeyBLE nonce and encrypted session flow.

This is the consolidated effective v29/v36/v37 behavior. Historical diagnostic and monkey-patch modules are not part of the runtime anymore.

## Home Assistant GATT / ESPHome path

For non-local Home Assistant paths, `HomeAssistantGattTransport`:

1. waits for a fresh connectable advertisement;
2. asks Home Assistant for the current connectable `BLEDevice`;
3. connects through `bleak-retry-connector`;
4. discovers the Eqiva send and receive characteristics;
5. registers notifications;
6. hands received bytes to the transport-independent KeyBLE protocol.

### ESPHome notification special case

The Eqiva lock rejects the descriptor write normally performed by the current ESPHome/Bleak notification setup before the KeyBLE nonce exchange with ATT error `0x05` (`Insufficient authentication`).

For an ESPHome backend the integration therefore registers the proxy-side notification callback without forcing that protected CCCD write. This workaround is deliberately narrow: proxy discovery, connection establishment and GATT writes still use Home Assistant's Bluetooth/Bleak path.

The proxy notification mode is exposed in diagnostics as `ESPHomeLocalOnly`.

## GATT writes

On the ESPHome/Home Assistant GATT backend, Eqiva uses the characteristic's normal `write` capability and Bleak `write_gatt_char(..., response=True)`.

On the local Raw ATT backend, the Eqiva-specific client emits opcode `0x12` (ATT Write Request) directly and deliberately does not wait for the ATT Write Response transaction. This preserves the hardware-confirmed local behavior.

The integration never substitutes an ATT Write Command for a motor-command fragment.

## Command safety

Connection/session setup may be retried before a motor command is sent. Once a motor `COMMAND` message has been sent, it is never automatically replayed after an ambiguous Bluetooth error. The lock may already be moving even if the client did not receive a clean completion response.

This invariant is covered by the unit test `test_motor_command_is_not_repeated_after_ambiguous_write`. The local wire semantics are additionally guarded by `test_raw_v37_sends_real_write_request_without_response_waiter`.

## Live mode

Live mode keeps the BLE and KeyBLE session open. It provides immediate processing of `STATUS_CHANGED`, an automatic status keepalive after at most three idle minutes and bounded reconnect backoff after a genuine disconnect.

Energy-saving mode connects only for a status update or command and closes the session afterwards.

## Diagnostics

Home Assistant diagnostics include:

- selected internal backend (`raw_att` or `ha_gatt`)
- path type (`local_raw_att`, `esphome_proxy`, `local_bluez` or Home Assistant GATT)
- Bluetooth source / adapter when available
- RSSI
- notification mode
- current connection state
- last coordinator result/error
- latest decoded lock status

Bluetooth address, user ID and user key are redacted.

## Hardware-confirmed state

Real Eqiva 142950A0 hardware has confirmed:

- local Linux/BlueZ operation through the consolidated Raw ATT compatibility path;
- ESPHome Bluetooth Proxy operation through Home Assistant GATT;
- encrypted status requests;
- live notifications;
- unlock;
- lock.

The b4 local BlueZ-GATT-only experiment is retained in Git history as evidence for why the local compatibility backend is still required.
