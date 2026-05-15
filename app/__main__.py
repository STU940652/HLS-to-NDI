"""Allow `python -m app`."""

from __future__ import annotations

from app.main import main

raise SystemExit(main())
