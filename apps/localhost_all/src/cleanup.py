from datetime import datetime, timedelta
from pathlib import Path

_PATTERNS = [
    "board_*.png",
    "board_undo_*.png",
    "board_restored_*.png",
    "evaluation_*.png",
]
_MAX_AGE_DAYS = 7


def cleanup_old_images(static_dir: str) -> int:
    """Delete board/evaluation PNGs older than _MAX_AGE_DAYS. Returns count deleted."""
    cutoff = datetime.now() - timedelta(days=_MAX_AGE_DAYS)
    deleted = 0
    for pattern in _PATTERNS:
        for f in Path(static_dir).rglob(pattern):
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                deleted += 1
    return deleted
