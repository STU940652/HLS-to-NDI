"""PyInstaller runtime hook: replicate gstreamer_bundle.pth environment setup.

The wheel stack expects `gstreamer_libs.setup_python_environment()` before PyGObject
imports. Normal installs get that from site-packages `.pth`; frozen apps do not.
"""
from __future__ import annotations

import gstreamer_libs

gstreamer_libs.setup_python_environment()
