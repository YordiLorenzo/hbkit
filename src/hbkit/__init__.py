"""hbkit - recover files from Synology Hyper Backup (.hbk) archives without Synology software."""

__version__ = "0.1.1"

from .archive import Archive, UnsupportedArchive, find_archive_root, is_archive  # noqa: F401

__all__ = ["Archive", "UnsupportedArchive", "find_archive_root", "is_archive", "__version__"]
