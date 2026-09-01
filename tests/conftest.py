from __future__ import annotations

import sys
import types
from pathlib import Path

# Import protocol modules without executing the Home Assistant integration's
# package __init__.py. The tests need no running Home Assistant instance,
# Bluetooth adapter, or personal Key Card data.
PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "eqiva_keyble"
package = types.ModuleType("custom_components.eqiva_keyble")
package.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.eqiva_keyble", package)
