from __future__ import annotations

from custom_components.eqiva_keyble.transport import TransportType
from custom_components.eqiva_keyble.transport_selection import (
    RSSI_SWITCH_MARGIN_DB,
    choose_transport_kind,
)


def _choose(
    *,
    local_available: bool = True,
    local_rssi: int | None = -70,
    nonlocal_available: bool = True,
    nonlocal_rssi: int | None = -70,
    previous_kind: TransportType | None = None,
):
    return choose_transport_kind(
        local_available=local_available,
        local_rssi=local_rssi,
        nonlocal_available=nonlocal_available,
        nonlocal_rssi=nonlocal_rssi,
        previous_kind=previous_kind,
    )


def test_initial_selection_uses_stronger_path() -> None:
    assert _choose(local_rssi=-82, nonlocal_rssi=-58).kind == TransportType.HA_GATT
    assert _choose(local_rssi=-55, nonlocal_rssi=-78).kind == TransportType.RAW_ATT


def test_initial_selection_prefers_local_on_equal_rssi() -> None:
    assert _choose(local_rssi=-70, nonlocal_rssi=-70).kind == TransportType.RAW_ATT


def test_selection_uses_only_available_path() -> None:
    assert (
        _choose(local_available=True, nonlocal_available=False).kind
        == TransportType.RAW_ATT
    )
    assert (
        _choose(local_available=False, nonlocal_available=True).kind
        == TransportType.HA_GATT
    )


def test_raw_att_hysteresis_requires_meaningfully_stronger_proxy() -> None:
    assert (
        _choose(
            local_rssi=-70,
            nonlocal_rssi=-70 + RSSI_SWITCH_MARGIN_DB - 1,
            previous_kind=TransportType.RAW_ATT,
        ).kind
        == TransportType.RAW_ATT
    )
    assert (
        _choose(
            local_rssi=-70,
            nonlocal_rssi=-70 + RSSI_SWITCH_MARGIN_DB,
            previous_kind=TransportType.RAW_ATT,
        ).kind
        == TransportType.HA_GATT
    )


def test_ha_gatt_hysteresis_requires_meaningfully_stronger_local_path() -> None:
    assert (
        _choose(
            local_rssi=-70 + RSSI_SWITCH_MARGIN_DB - 1,
            nonlocal_rssi=-70,
            previous_kind=TransportType.HA_GATT,
        ).kind
        == TransportType.HA_GATT
    )
    assert (
        _choose(
            local_rssi=-70 + RSSI_SWITCH_MARGIN_DB,
            nonlocal_rssi=-70,
            previous_kind=TransportType.HA_GATT,
        ).kind
        == TransportType.RAW_ATT
    )


def test_no_current_path_delegates_discovery_to_home_assistant() -> None:
    choice = _choose(local_available=False, nonlocal_available=False)

    assert choice.kind == TransportType.HA_GATT
