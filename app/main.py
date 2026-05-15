#!/usr/bin/env python3
"""Application entrypoint."""

from __future__ import annotations

from app.ui import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
