from __future__ import annotations

from typing import Any


def normalize_knx_group_address(value: Any) -> str:
    """Validate and normalize a KNX free-, two- or three-level address."""
    address = str(value or "").strip()
    if not address:
        return ""

    parts = address.split("/")
    if len(parts) not in (1, 2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError("invalid KNX group address")

    levels = [int(part) for part in parts]
    if len(levels) == 1:
        valid = 0 <= levels[0] <= 65535
    elif len(levels) == 2:
        valid = 0 <= levels[0] <= 31 and 0 <= levels[1] <= 2047
    else:
        valid = (
            0 <= levels[0] <= 31
            and 0 <= levels[1] <= 7
            and 0 <= levels[2] <= 255
        )

    if not valid:
        raise ValueError("invalid KNX group address")
    return "/".join(str(level) for level in levels)
