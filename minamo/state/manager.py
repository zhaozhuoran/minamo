"""StateManager: the single, program-maintained state store.

Unlike ``config/`` (operator-authored) or ``data/localdisk`` (user content),
*state* is written by Minamo itself at runtime — e.g. OAuth access/refresh
tokens that are automatically refreshed. State lives under ``data/state/`` and
is gitignored. Because it can contain secrets (refresh tokens), written files
are created with ``0600`` permissions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class StateManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str) -> Path:
        return self.root / f"{namespace}.json"

    def read(self, namespace: str) -> dict:
        path = self._path(namespace)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, namespace: str, data: dict) -> None:
        path = self._path(namespace)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort, e.g. Windows
            pass

    def update(self, namespace: str, **patch: object) -> dict:
        data = self.read(namespace)
        data.update(patch)
        self.write(namespace, data)
        return data
