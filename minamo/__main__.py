"""Minamo runnable entry point.

Launch with:  python -m minamo
(or: uvicorn minamo.app:app --port 8000)
"""
from __future__ import annotations

import uvicorn

from .app import create_app
from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "minamo.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
