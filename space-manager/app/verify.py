"""Local-backup verification.

An asset may only be deleted from iCloud once we can point at a file in the
local backup tree that matches it. Matching mirrors how icloudpd names files
on disk (default name-size-dedup-with-suffix policy):

  IMG_1234.MOV                exact filename
  IMG_1234-<bytes>.MOV        dedup suffix icloudpd adds on name collisions
  IMG_1234-original.MOV       legacy suffix from older icloudpd versions

and in every case the on-disk byte size must equal the iCloud original size.
Filename comparison is case-insensitive because the tree may live on
case-insensitive shares (SMB).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


@dataclass
class LocalIndex:
    # lowercased basename -> list of (size, absolute path)
    by_name: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    built_at: float = 0.0
    file_count: int = 0
    roots: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


def build_index(dirs: tuple[str, ...] | list[str]) -> LocalIndex:
    index = LocalIndex(roots=tuple(dirs), built_at=time.time())
    for root in dirs:
        if not os.path.isdir(root):
            index.errors.append(f"backup dir not found: {root}")
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                index.by_name.setdefault(name.lower(), []).append((size, path))
                index.file_count += 1
    return index


def _candidate_names(filename: str, size: int) -> list[str]:
    stem, ext = os.path.splitext(filename)
    return [
        filename.lower(),
        f"{stem}-{size}{ext}".lower(),
        f"{stem}-original{ext}".lower(),
    ]


def find_local_copy(index: LocalIndex, filename: str, size: int) -> str | None:
    """Return the path of a verified local copy, or None."""
    for name in _candidate_names(filename, size):
        for local_size, path in index.by_name.get(name, ()):
            if local_size == size:
                return path
    return None
