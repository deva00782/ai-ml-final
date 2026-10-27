"""
Desktop PC Automation Engine
Manages JioSaavn browser automation via Selenium, OS volume control, and media keys.
"""

import sys
from pathlib import Path

# Ensure desktop module root is on sys.path for internal un-prefixed imports
_desktop_dir = str(Path(__file__).resolve().parent)
if _desktop_dir not in sys.path:
    sys.path.insert(0, _desktop_dir)

try:
    from .desktop_controller import register_desktop_backend
except ImportError:
    from desktop_controller import register_desktop_backend

# Backward compatibility aliases for legacy imports
sys.modules.setdefault("desktop_dashboard", sys.modules[__name__])
sys.modules.setdefault("desktop_dashboard.desktop_modules", sys.modules[__name__])
sys.modules.setdefault("desktop_controller", sys.modules.get(f"{__name__}.desktop_controller"))
