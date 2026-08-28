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
- Live mode with persistent KeyBLE session and immediate manual status changes
- Automatic reconnect with bounded backoff in live mode
- One safe connection/session retry before an operation starts


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
