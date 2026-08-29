# Eqiva Bluetooth Smart Lock for Home Assistant


Native Home Assistant integration for the **eQ-3 / Eqiva Bluetooth Smart Lock
(Key-BLE, 142950A0)**.


The integration communicates locally over Bluetooth. No MQTT broker, Node.js
service or cloud account is required.


> [!WARNING]
> Keep a physical key available while testing. Never publish Key Card QR data,
> user IDs or user keys in issues, logs, screenshots or chat messages.


## Features


- Native Home Assistant Bluetooth integration
- Setup through the Home Assistant UI
- Pairing with the original Eqiva Key Card
- Optional setup with an existing KeyBLE user ID and 128-bit user key
- Native `lock` entity with lock, unlock and open-latch commands
- Immediate locking, unlocking and opening transition states
- Translated battery status sensor (`OK` / `Low`, `i.O.` / `Schwach`)
- Configurable full status synchronization from 1 to 60 minutes
- Energy-saving polling mode with connections only when required
- Live mode with persistent KeyBLE session, 3-minute keepalive and immediate manual status changes
- Automatic reconnect with bounded backoff in live mode
- One safe connection/session retry before an operation starts
- Optional KNX/IP bridge with freely configurable group addresses


Motor commands are deliberately never retried after they have been sent. A
Bluetooth timeout after a command can be ambiguous because the lock may already
be moving.


## Installation with HACS


1. Open **HACS** in Home Assistant.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/DerHimbeerHugo/home-assistant-eqiva-keyble`.
4. Select repository type **Integration**.
5. Install **Eqiva Bluetooth Smart Lock**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration** and search for
   **Eqiva Bluetooth Smart Lock**.


HACS installs stable versions from GitHub releases. New development work remains
on dedicated development branches until it has passed real-hardware testing.


## Pairing with the Eqiva Key Card


The QR code on the original Key Card contains the Bluetooth address and card key
needed to register a new user on the lock.


1. Start the integration setup and choose **Mit Eqiva Key Card koppeln**.
2. Enter a name and the complete QR-code data locally in Home Assistant.
3. Hold the **unlock** button on the lock until the yellow LED flashes.
4. Submit the form.


The Key Card data itself is not persisted after successful pairing. The newly
registered user ID and user key are stored in the Home Assistant config entry
because they are required for future encrypted communication with the lock.

## Connection modes

The mode and synchronization interval can be changed under
**Settings → Devices & services → Eqiva Bluetooth Smart Lock → Configure**.

### Energy saving (default)

Home Assistant connects only for a status update or command and disconnects
afterwards. The default interval is 10 minutes and can be configured from 1 to
60 minutes.

### Live

Home Assistant keeps the BLE and KeyBLE session open. Manual changes at the lock
are reported immediately, and an unexpected disconnect starts an automatic
reconnect with bounded backoff. An independent status keepalive runs after at
most three idle minutes, matching the proven ESPHome setup and preventing the
lock's roughly four-minute idle timeout. Any successful command or status
traffic restarts that keepalive timer. The configured interval remains active
as an additional full status synchronization.

The Eqiva lock accepts only a limited number of simultaneous Bluetooth
connections. The official Eqiva app or another KeyBLE client may therefore be
unable to connect while live mode is active. Live mode can also increase battery
usage compared with energy-saving mode.

## KNX/IP bridge (v0.3 beta)

The optional KNX bridge uses Home Assistant's existing KNX/IP connection; it
does not open a second tunnel. Enable it under **Settings → Devices & services →
Eqiva Bluetooth Smart Lock → Configure**. Home Assistant then adds editable KNX
group-address fields to the **Configuration** section of the lock's device page.
Enter only the addresses you need. Free-level, two-level and three-level KNX
group-address formats are accepted. Existing addresses entered with v0.3.0b1
are retained automatically.

All KNX objects use DPT 1. Lock, unlock and open-latch commands have separate
addresses and react only to an incoming value `1`; value `0`, responses and
outgoing telegrams are ignored. Optional status addresses report locked,
battery-low and availability states and answer GroupValueRead requests.

## Bluetooth requirements

The raw ATT transport used for this lock requires a **local Linux/BlueZ Bluetooth
adapter** available to Home Assistant as an `hci` adapter. A Bluetooth proxy may
discover the lock, but it cannot provide the local raw L2CAP/ATT connection path
required by this integration.

Close the official Eqiva app and stop other KeyBLE bridges while pairing or when
diagnosing connection problems.

## What's new in v0.2.0

- Selectable energy-saving and live connection modes
- Configurable 1–60 minute synchronization interval
- Persistent live session with push updates and automatic reconnect
- Safe two-attempt session preparation synchronized to fresh advertisements
- 15-second raw L2CAP connection timeout
- Immediate transition states for lock commands
- Battery status sensor replacing the two legacy diagnostic binary sensors
- Existing v0.1.x configuration entries migrate without being recreated

## Protocol / credits

The Key-BLE protocol implementation is based on the reverse engineering from
the ISC-licensed [`oyooyo/keyble`](https://github.com/oyooyo/keyble) project. The
command IDs, message framing, AES-128 authentication/encryption and pairing flow
are ported to Python and Home Assistant's Bluetooth stack.

## Tested hardware

- eQ-3 / Eqiva Bluetooth Smart Lock
- Model / article number: **142950A0**

## License

ISC License. See [LICENSE](LICENSE).
