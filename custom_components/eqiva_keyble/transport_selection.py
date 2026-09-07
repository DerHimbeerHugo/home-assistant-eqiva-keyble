from __future__ import annotations

from dataclasses import dataclass

from .transport import TransportType

RSSI_SWITCH_MARGIN_DB = 6


@dataclass(frozen=True, slots=True)
class TransportChoice:
    """Result of one automatic Bluetooth path selection."""

    kind: TransportType
    reason: str


def _rssi_value(rssi: int | None) -> int:
    """Normalize an unknown RSSI to the weakest useful comparison value."""
    return rssi if rssi is not None else -127


def choose_transport_kind(
    *,
    local_available: bool,
    local_rssi: int | None,
    nonlocal_available: bool,
    nonlocal_rssi: int | None,
    previous_kind: TransportType | None,
    switch_margin_db: int = RSSI_SWITCH_MARGIN_DB,
) -> TransportChoice:
    """Choose the Bluetooth backend with hysteresis between reconnects."""
    if not local_available and not nonlocal_available:
        return TransportChoice(
            TransportType.HA_GATT,
            "no current connectable path; let Home Assistant discover one",
        )
    if local_available and not nonlocal_available:
        return TransportChoice(TransportType.RAW_ATT, "only local hci path available")
    if nonlocal_available and not local_available:
        return TransportChoice(
            TransportType.HA_GATT,
            "only non-local Home Assistant path available",
        )

    local_value = _rssi_value(local_rssi)
    nonlocal_value = _rssi_value(nonlocal_rssi)

    if previous_kind == TransportType.RAW_ATT:
        if nonlocal_value >= local_value + switch_margin_db:
            return TransportChoice(
                TransportType.HA_GATT,
                f"non-local path is at least {switch_margin_db} dB stronger",
            )
        return TransportChoice(
            TransportType.RAW_ATT,
            f"keep local path within {switch_margin_db} dB hysteresis",
        )

    if previous_kind == TransportType.HA_GATT:
        if local_value >= nonlocal_value + switch_margin_db:
            return TransportChoice(
                TransportType.RAW_ATT,
                f"local path is at least {switch_margin_db} dB stronger",
            )
        return TransportChoice(
            TransportType.HA_GATT,
            f"keep non-local path within {switch_margin_db} dB hysteresis",
        )

    if nonlocal_value > local_value:
        return TransportChoice(TransportType.HA_GATT, "non-local path has stronger RSSI")
    return TransportChoice(TransportType.RAW_ATT, "local path has equal or stronger RSSI")
