"""Temporary Workspace (tmp) independent module.

Provides temporary workspace functionality under data_root / "tmp".
Not associated with any storage backend. Tracks temporary disk usage and throws
exceptions if max temporary space is exceeded.
"""
from __future__ import annotations

import os
import shutil
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger("minamo.utils.temp_workspace")


class TempWorkspace:
    def __init__(self, root: Path, max_usage_bytes: int = 5 * 1024 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_usage_bytes = max_usage_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def get_current_usage(self) -> int:
        """Returns the total size of files inside the temporary workspace in bytes."""
        total = 0
        if self.root.exists():
            for dirpath, _, filenames in os.walk(self.root):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        return total

    @contextmanager
    def allocate_file(self, prefix: str = "tmp_", suffix: str = ""):
        """Context manager to allocate a temporary file and delete it on completion.
        Raises OSError if the temp workspace has reached its limit.
        """
        import tempfile
        current_usage = self.get_current_usage()
        if current_usage >= self.max_usage_bytes:
            raise OSError(
                f"Temporary workspace exceeded maximum capacity limit of {self.max_usage_bytes} bytes "
                f"(Current usage: {current_usage} bytes)."
            )

        fd, path_str = tempfile.mkstemp(dir=str(self.root), prefix=prefix, suffix=suffix)
        os.close(fd)
        path = Path(path_str)
        try:
            yield path
        finally:
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete temp file {path}: {e}")

    def clean_all(self) -> None:
        """Removes all files and subdirectories under the temporary workspace root."""
        if self.root.exists():
            for child in self.root.iterdir():
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)
                except Exception as e:
                    logger.warning(f"Failed to clean temporary workspace item {child}: {e}")
