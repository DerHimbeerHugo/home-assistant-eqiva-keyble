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
- Native `lock` entity with lock, unlock and open-latch commands
- Immediate locking, unlocking and opening transition states
- Translated battery status sensor (`OK` / `Low`, `i.O.` / `Schwach`)
- Configurable 1–60 minute status synchronization in energy-saving mode
- Energy-saving polling mode with connections only when required
- Live mode with persistent KeyBLE session, 3-minute keepalive and immediate manual status changes
- Automatic reconnect with bounded backoff in live mode
- One safe connection/session retry before an operation starts
- Selectable Raw ATT reference and experimental Home Assistant GATT transport
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


1. Start the integration setup; the Key Card form opens directly.
2. Enter a name and the complete QR-code data locally in Home Assistant.
3. Select the Bluetooth connection mode, Bluetooth transport and optionally
   enable KNX.
4. Hold the **unlock** button on the lock until the yellow LED flashes.
5. Submit the form.


The Key Card data itself is not persisted after successful pairing. The newly
registered user ID and user key are stored in the Home Assistant config entry
because they are required for future encrypted communication with the lock.

## Connection modes

The mode can be changed under **Settings → Devices & services → Eqiva Bluetooth
Smart Lock → Configure**. The synchronization interval is shown only while
energy-saving mode is active.

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
traffic restarts that keepalive timer. This keepalive is the only scheduled
status synchronization in live mode, so no separate polling interval is shown.

The Eqiva lock accepts only a limited number of simultaneous Bluetooth
connections. The official Eqiva app or another KeyBLE client may therefore be
unable to connect while live mode is active. Live mode can also increase battery
usage compared with energy-saving mode.

## Bluetooth transports (v0.4 development)

The transport can be changed under **Settings → Devices & services → Eqiva
Bluetooth Smart Lock → Configure**. Existing installations and new setups
default to **Raw ATT** so the known-working path does not change silently.

- **Raw ATT** is the proven reference. It requires a local Linux/BlueZ `hci`
  adapter, keeps MTU 23, and preserves the Eqiva-specific v29/v36/v37 timing.
- **HA GATT** is experimental. Home Assistant supplies the connectable path, so
  it can in principle be a local adapter or an ESPHome Bluetooth Proxy. An
  explicit HA-GATT selection never falls back to Raw ATT.

For the first ESPHome Bluetooth Proxy test, explicitly select **HA GATT –
experimental / proxy test**. The selected transport is written to the debug
log, and an HA-GATT connection error is returned unchanged instead of being
hidden by the local Raw-ATT path.

The proxy path is not yet hardware-confirmed. In particular, normal Bleak/proxy
GATT can send a true Write Request only while also waiting for its ATT Write
Response. Raw v37 sends the same request opcode but does not wait for that
response because the KeyBLE notification may arrive first. HA GATT deliberately
does not replace this with a Write Command; the real lock test must show whether
the normal response wait is acceptable.

## KNX/IP bridge

The optional KNX bridge uses Home Assistant's existing KNX/IP connection; it
does not open a second tunnel. Enable it directly during initial setup or later
under **Settings → Devices & services → Eqiva Bluetooth Smart Lock → Configure**.
Home Assistant then adds editable KNX group-address fields to the
**Configuration** section of the lock's device page.
Enter only the addresses you need. Free-level, two-level and three-level KNX
group-address formats are accepted. Existing addresses entered with v0.3.0b1
are retained automatically.

All KNX objects use DPT 1.001. Lock, unlock and open-latch commands have separate
addresses and react only to an incoming value `1`; value `0`, responses and
outgoing telegrams are ignored. Optional status addresses report locked,
battery-low and availability states and answer GroupValueRead requests.

## Bluetooth requirements

Raw ATT requires a **local Linux/BlueZ Bluetooth adapter** available to Home
Assistant as an `hci` adapter. The experimental HA-GATT transport instead uses
Home Assistant's connectable Bluetooth abstraction and has no artificial
`hciX` restriction. ESPHome Bluetooth Proxy operation remains unconfirmed until
the v0.4 path has been tested on the real lock.

Close the official Eqiva app and stop other KeyBLE bridges while pairing or when
diagnosing connection problems.

The detailed runtime mapping and retained monkey-patch history are documented
in [`docs/transport-architecture.md`](docs/transport-architecture.md).

## What's new in v0.3.2

- Allow KNX to be enabled directly during initial Key Card setup
- Preserve the selected KNX state in the newly created configuration entry
- Include the clarified QR-code, OK-button and live-mode guidance in a new release

## What's new in v0.3.1

- Register configured KNX event addresses explicitly with DPT 1.001
- Supply the required `remove: false` flag during KNX event registration
- Send all KNX command and status objects consistently as DPT 1.001
- Add focused debug output for KNX registration and received Eqiva telegrams
- Clarify Key Card entry and the latency benefit of live mode during setup

## What's new in v0.3.0

- Optional KNX/IP bridge using Home Assistant's existing KNX connection
- Six freely configurable DPT 1 command and status group addresses on the device page
- GroupValueRead responses for configured KNX status objects
- Automatic 3-minute keepalive and reconnect for persistent live sessions
- No redundant polling interval in live mode; energy-saving polling remains configurable
- New installations start directly with the original Key Card QR code
- Existing configuration entries upgrade without being recreated

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
