# Eqiva Bluetooth Smart Lock for Home Assistant

Native Home Assistant integration for the **eQ-3 / Eqiva Bluetooth Smart Lock (Key-BLE, 142950A0)**.

The integration communicates locally over Home Assistant's Bluetooth stack. No MQTT broker, Node.js service or cloud account is required.

> [!WARNING]
> Keep a physical key available while testing. Never publish Key Card QR data, user IDs or user keys in issues, logs, screenshots or chat messages.

## Features

- Native Home Assistant Bluetooth integration
- Setup through the Home Assistant UI
- Pairing with the original Eqiva Key Card
- Native `lock` entity with lock, unlock and open-latch commands
- Immediate locking, unlocking and opening transition states
- Battery status sensor
- Energy-saving polling mode with configurable 1–60 minute status synchronization
- Live mode with persistent KeyBLE session, 3-minute keepalive and immediate manual status changes
- Automatic reconnect with bounded backoff in live mode
- One safe connection/session retry before an operation starts
- Home Assistant-selected local Bluetooth adapters and ESPHome Bluetooth Proxies
- Optional KNX/IP bridge with freely configurable group addresses
- Privacy-safe Home Assistant diagnostics for Bluetooth path, RSSI and runtime state

Motor commands are deliberately never retried after they have been sent. A Bluetooth timeout after a command can be ambiguous because the lock may already be moving.

## Installation with HACS

1. Open **HACS** in Home Assistant.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/DerHimbeerHugo/home-assistant-eqiva-keyble`.
4. Select repository type **Integration**.
5. Install **Eqiva Bluetooth Smart Lock**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration** and search for **Eqiva Bluetooth Smart Lock**.

## Pairing with the Eqiva Key Card

The QR code on the original Key Card contains the Bluetooth address and card key needed to register a new user on the lock.

1. Start the integration setup; the Key Card form opens directly.
2. Enter a name and the complete QR-code data locally in Home Assistant.
3. Choose the connection mode and optionally enable KNX.
4. Hold the **unlock** button on the lock until the yellow LED flashes.
5. Submit the form.

There is no Bluetooth transport selection. The integration asks Home Assistant for a current connectable Bluetooth device and Home Assistant chooses the available path, including local adapters and ESPHome Bluetooth Proxies.

The Key Card data itself is not persisted after successful pairing. The newly registered user ID and user key are stored in the Home Assistant config entry because they are required for future encrypted communication with the lock.

## Connection modes

The mode can be changed under **Settings → Devices & services → Eqiva Bluetooth Smart Lock → Configure**.

### Energy saving (default)

Home Assistant connects only for a status update or command and disconnects afterwards. The default interval is 10 minutes and can be configured from 1 to 60 minutes.

### Live

Home Assistant keeps the BLE and KeyBLE session open. Manual changes at the lock are reported immediately, and an unexpected disconnect starts an automatic reconnect with bounded backoff. An independent status keepalive runs after at most three idle minutes and prevents the lock's roughly four-minute idle timeout.

The Eqiva lock accepts only a limited number of simultaneous Bluetooth connections. The official Eqiva app or another KeyBLE client may therefore be unable to connect while live mode is active. Live mode can also increase battery usage compared with energy-saving mode.

## Bluetooth architecture in v0.4

Version 0.4 uses one Bluetooth transport only: **Home Assistant GATT**.

The integration obtains a connectable BLE device from Home Assistant instead of opening its own Linux `hciX`/Raw ATT connection. This allows Home Assistant to select the usable Bluetooth path and keeps adapter/proxy handling inside the Home Assistant Bluetooth infrastructure.

The previously developed local Raw ATT implementation was removed before the public 0.4 release candidate. It was valuable during protocol reverse engineering, but maintaining a second low-level Linux-specific Bluetooth stack would add complexity and additional Home Assistant update risk without providing a user-facing benefit.

### ESPHome Bluetooth Proxy note

The Eqiva lock rejects the normal ESPHome/Bleak notification CCCD write before the KeyBLE nonce exchange with ATT error `0x05` (insufficient authentication). For ESPHome proxy connections the integration therefore uses a narrowly scoped notification registration workaround that skips that protected descriptor write while keeping discovery, connection and GATT communication on Home Assistant's Bluetooth path.

This ESPHome proxy path has been confirmed on real hardware for status reads, live notifications, locking and unlocking.

## KNX/IP bridge

The optional KNX bridge uses Home Assistant's existing KNX/IP connection; it does not open a second tunnel. Enable it directly during initial setup or later under **Settings → Devices & services → Eqiva Bluetooth Smart Lock → Configure**. Home Assistant then adds editable KNX group-address fields to the **Configuration** section of the lock's device page.

All KNX objects use DPT 1.001. Lock, unlock and open-latch commands have separate addresses and react only to an incoming value `1`. Optional status addresses report locked, battery-low and availability states and answer GroupValueRead requests.

## Diagnostics

Home Assistant can export integration diagnostics containing the selected Bluetooth backend/path type, source, RSSI, notification mode, connection state and the latest runtime result. Bluetooth address, user ID and user key are redacted.

## Bluetooth requirements

Home Assistant needs at least one connectable Bluetooth path that can reach the lock. This can be a supported local Bluetooth adapter or an ESPHome Bluetooth Proxy. Reliable operation still depends on usable BLE signal strength.

Close the official Eqiva app and stop other KeyBLE bridges while pairing or when diagnosing connection problems.

The runtime architecture is documented in [`docs/transport-architecture.md`](docs/transport-architecture.md).

## Protocol / credits

The Key-BLE protocol implementation is based on the reverse engineering from the ISC-licensed [`oyooyo/keyble`](https://github.com/oyooyo/keyble) project. The command IDs, message framing, AES-128 authentication/encryption and pairing flow are ported to Python and Home Assistant's Bluetooth stack.

## Tested hardware

- eQ-3 / Eqiva Bluetooth Smart Lock
- Model / article number: **142950A0**
- ESPHome Bluetooth Proxy through Home Assistant GATT: status, live updates and motor commands

## License

ISC License. See [LICENSE](LICENSE).
