from __future__ import annotations


class EqivaError(Exception):
    """Base error for the Eqiva KeyBLE integration."""


class EqivaProtocolError(EqivaError):
    """Protocol or authentication error."""


class EqivaNotFoundError(EqivaProtocolError):
    """Bluetooth device is not currently known to Home Assistant."""


class EqivaConnectionError(EqivaProtocolError):
    """Bluetooth transport setup or I/O failed."""


class EqivaHandshakeError(EqivaProtocolError):
    """The KeyBLE nonce handshake failed."""
