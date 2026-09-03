# Eqiva Key-BLE Bluetooth architecture

## Runtime architecture

```text
Home Assistant
    │
Eqiva Coordinator
    │
KeyBLE session / protocol
    │
HomeAssistantGattTransport
    │
Home Assistant Bluetooth API
    │
local adapter or ESPHome Bluetooth Proxy
    │
Eqiva Bluetooth Smart Lock
```

The KeyBLE protocol owns framing, AES authentication/encryption, nonces, counters, pairing, status requests and motor commands. Bluetooth discovery, adapter/proxy selection and the GATT connection are delegated to Home Assistant.

## One Bluetooth path

Version 0.4 intentionally has only one production Bluetooth transport: `HomeAssistantGattTransport`.

The earlier Raw ATT implementation opened Linux L2CAP/ATT sockets and selected local `hciX` adapters directly. It was essential while reverse engineering the Eqiva-specific ATT behavior, but it was removed before the public v0.4 release candidate. A second Linux-specific transport would duplicate Home Assistant's Bluetooth routing and increase maintenance risk.

There is no transport selector in the config flow or options flow. A normal user only chooses the KeyBLE connection mode (energy-saving or live). Home Assistant decides which connectable Bluetooth path reaches the lock.

## Discovery and connection

Before pairing, the config flow verifies that Home Assistant has at least one connectable Bluetooth scanner and that the lock is currently reachable. If necessary it requests a short active scan.

For a connection, `HomeAssistantGattTransport`:

1. waits for a fresh connectable advertisement;
2. asks Home Assistant for the current connectable `BLEDevice`;
3. connects through `bleak-retry-connector`;
4. discovers the Eqiva send and receive characteristics;
5. registers notifications;
6. hands received bytes to the transport-independent KeyBLE protocol.

The returned device may represent a local adapter or a remote ESPHome Bluetooth Proxy. The integration does not filter the path to a local `hciX` adapter.

## Eqiva notification special case

The Eqiva lock rejects the descriptor write normally performed by the current ESPHome/Bleak notification setup before the KeyBLE nonce exchange with ATT error `0x05` (`Insufficient authentication`).

For an ESPHome backend the integration therefore registers the proxy-side notification callback without forcing that protected CCCD write. This workaround is deliberately narrow: adapter/proxy discovery, connection establishment and GATT writes still use Home Assistant's Bluetooth/Bleak path.

The proxy notification mode is exposed in diagnostics as `ESPHomeLocalOnly`.

## GATT writes

Eqiva commands require the characteristic's normal `write` capability. The transport uses Bleak `write_gatt_char(..., response=True)`, which maps to the GATT Write Request path exposed by the portable Home Assistant/Bleak stack.

The integration never changes to `write-without-response` as a fallback, because that is a different ATT operation.

## Command safety

Connection/session setup may be retried before a motor command is sent. Once a motor `COMMAND` message has been sent, it is never automatically replayed after an ambiguous Bluetooth error. The lock may already be moving even if the client did not receive a clean completion response.

This invariant is covered by the unit test `test_motor_command_is_not_repeated_after_ambiguous_write`.

## Live mode

Live mode keeps the BLE and KeyBLE session open. It provides immediate processing of `STATUS_CHANGED`, an automatic status keepalive after at most three idle minutes and bounded reconnect backoff after a genuine disconnect.

Energy-saving mode connects only for a status update or command and closes the session afterwards.

## Diagnostics

Home Assistant diagnostics include:

- Bluetooth transport (`ha_gatt`)
- backend/path type (`local_bluez`, `esphome_proxy` or `unknown`)
- Home Assistant Bluetooth source
- RSSI
- notification mode
- current connection state
- last coordinator result/error
- latest decoded lock status

Bluetooth address, user ID and user key are redacted.

## Hardware-confirmed state

The Home Assistant GATT path has been confirmed on real Eqiva 142950A0 hardware through an ESPHome Bluetooth Proxy for:

- pairing/session establishment
- encrypted status requests
- live notifications
- unlock
- lock

The same transport is designed to use a Home Assistant-selected local adapter as well; external testing across additional Home Assistant Bluetooth setups is part of the v0.4 validation process.
