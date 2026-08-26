# Eqiva Bluetooth Smart Lock for Home Assistant

Experimental native Home Assistant integration for the **eQ-3 / Eqiva Bluetooth Smart Lock (Key-BLE, 142950A0)**.

The integration communicates locally over Bluetooth. No MQTT broker, Node.js service or cloud account is required.

> [!WARNING]
> This is an early hardware-test release. Keep a physical key available while testing and do not publish Key Card QR data, user IDs or user keys.

## Features

- Native Home Assistant Bluetooth stack
- Home Assistant UI config flow
- Pairing with the original Eqiva Key Card
- Existing KeyBLE user ID + 128-bit user key
- Native `lock` entity
- Lock
- Unlock
- Open / retract latch
- Lock state
- Battery-low diagnostic
- Pairing-allowed diagnostic

## Installation with HACS

1. Open **HACS** in Home Assistant.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/DerHimbeerHugo/home-assistant-eqiva-keyble`.
4. Select repository type **Integration**.
5. Install **Eqiva Bluetooth Smart Lock**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration** and search for **Eqiva Bluetooth Smart Lock**.

For this initial test version HACS installs directly from the default `main` branch; a GitHub release is not required.

## Pairing with the Eqiva Key Card

The QR code on the original Key Card contains the Bluetooth address and card key needed to register a new user on the lock.

1. Start the integration setup and choose **Mit Eqiva Key Card koppeln**.
2. Enter a name and the complete QR-code data locally in Home Assistant.
3. Hold the **unlock** button on the lock until the yellow LED flashes.
4. Submit the form.

The Key Card data itself is not persisted after successful pairing. The newly registered user ID and user key are stored in the Home Assistant config entry because they are required for future encrypted communication with the lock.

### Security

Treat the Key Card QR data and generated user key like door-lock credentials. Do not post them in issues, logs, screenshots or chat messages.

## Bluetooth requirements

Home Assistant must see the lock through a **connectable** Bluetooth adapter or Bluetooth proxy. The lock only handles a limited number of concurrent BLE connections, so close the official Eqiva app while testing this integration.

Status polling is intentionally limited to every 10 minutes to reduce Bluetooth traffic and battery usage. Commands trigger immediate status reads.

## Protocol / credits

The Key-BLE protocol implementation is based on the reverse engineering from the ISC-licensed [`oyooyo/keyble`](https://github.com/oyooyo/keyble) project. The command IDs, message framing, AES-128 authentication/encryption and pairing flow are ported to Python and Home Assistant's Bluetooth stack.

## Tested hardware

Target hardware:

- eQ-3 / Eqiva Bluetooth Smart Lock
- Model / article number: **142950A0**

Real-hardware validation is currently in progress.

## License

ISC License. See [LICENSE](LICENSE).
