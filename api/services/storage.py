"""Async-safe centralized JSON storage service.

Uses asyncio.Lock (not threading.Lock) for FastAPI async compatibility.
Uses aiofiles for non-blocking file I/O.
"""
import asyncio
import json
import aiofiles
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class StorageService:
    """Thread-safe async storage for JSON state files.

    Uses asyncio.Lock for async safety in FastAPI context.
    Each instance has its own lock to prevent cross-directory conflicts.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._lock = asyncio.Lock()  # Instance-level async lock
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _validate_filename(self, filename: str) -> Path:
        """Prevent path traversal attacks.

        Blocks:
        - Filenames containing '..'
        - Filenames starting with '/'
        - Any path that resolves outside base_dir
        """
        if ".." in filename or "/" in filename:
            raise ValueError(f"Invalid filename: {filename}")
        path = self.base_dir / filename
        # Ensure resolved path is within base_dir
        if not path.resolve().is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"Path traversal detected: {filename}")
        return path

    async def _load_unlocked(self, path: Path, default: Any = None) -> Any:
        """Load JSON file without lock (for internal use only)."""
        if not path.exists():
            return default if default is not None else {}
        try:
            async with aiofiles.open(path, mode='r') as f:
                content = await f.read()
                return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted JSON in {path.name}: {e}")
            return default if default is not None else {}

    async def load(self, filename: str, default: Any = None) -> Any:
        """Load JSON file with async lock protection.

        Args:
            filename: Name of file in base_dir (no path separators allowed)
            default: Value to return if file doesn't exist or is corrupted

        Returns:
            Parsed JSON content or default value
        """
        path = self._validate_filename(filename)
        async with self._lock:
            return await self._load_unlocked(path, default)

    async def _save_unlocked(self, path: Path, data: Any) -> None:
        """Save JSON file atomically without lock (for internal use only)."""
        temp_path = path.with_suffix('.tmp')
        try:
            async with aiofiles.open(temp_path, mode='w') as f:
                await f.write(json.dumps(data, indent=2, default=str))
            temp_path.rename(path)  # Atomic on POSIX
        except Exception as e:
            logger.error(f"Failed to save {path.name}: {e}")
            if temp_path.exists():
                temp_path.unlink()
            raise

    async def save(self, filename: str, data: Any) -> None:
        """Save JSON file atomically with async lock protection.

        Uses temp file + rename for atomicity on POSIX systems.
        """
        path = self._validate_filename(filename)
        async with self._lock:
            await self._save_unlocked(path, data)

    async def append_to_list(self, filename: str, item: Dict, max_items: int = 1000) -> None:
        """Append item to list file with automatic truncation.

        Adds timestamp to each item and maintains max_items limit.
        Holds lock for entire read-modify-write cycle to prevent race conditions.
        """
        path = self._validate_filename(filename)
        async with self._lock:
            data = await self._load_unlocked(path, [])
            data.append({**item, "timestamp": datetime.now().isoformat()})
            if len(data) > max_items:
                data = data[-max_items:]
            await self._save_unlocked(path, data)

    def load_sync(self, filename: str, default: Any = None) -> Any:
        """Synchronous load - for startup/shutdown contexts only.

        WARNING: Do not use in async request handlers.
        """
        path = self._validate_filename(filename)
        if not path.exists():
            return default if default is not None else {}
        with open(path) as f:
            return json.load(f)
