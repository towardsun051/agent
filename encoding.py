"""Shared encoding utilities — safe file reading with fallback."""

from pathlib import Path


def safe_read_text(path: Path) -> str:
    """Read a text file with encoding fallback: UTF-8 → GBK → UTF-8(replace).

    On Windows, files created by terminal tools or external programs may use
    GBK encoding instead of UTF-8. This function handles those gracefully.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return path.read_text(encoding="gbk")
    except UnicodeDecodeError:
        pass
    return path.read_text(encoding="utf-8", errors="replace")
