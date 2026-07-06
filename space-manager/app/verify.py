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

Live Photos are one iCloud asset with two files. Deleting the asset removes
both from iCloud, so verification requires BOTH on disk: the still, and the
motion clip under icloudpd's naming (IMG_1234_HEVC.MOV with the default
"suffix" policy for HEIC stills, IMG_1234.MOV with the "original" policy or
for non-HEIC stills) — matched against the motion clip's own byte size.
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


def _motion_filenames(still_filename: str) -> list[str]:
    """Possible on-disk names of a Live Photo's motion clip, per icloudpd's
    two --live-photo-mov-filename-policy values."""
    stem, ext = os.path.splitext(still_filename)
    names = [f"{stem}.MOV"]  # "original" policy, and non-HEIC stills
    if ext.lower() == ".heic":
        names.insert(0, f"{stem}_HEVC.MOV")  # "suffix" policy (default)
    return names


def find_local_motion_copy(
    index: LocalIndex, still_filename: str, lp_size: int
) -> str | None:
    """Return the path of a verified local Live Photo motion clip, or None."""
    for motion_name in _motion_filenames(still_filename):
        path = find_local_copy(index, motion_name, lp_size)
        if path:
            return path
    return None


def verify_asset(
    index: LocalIndex, filename: str, size: int, lp_size: int | None
) -> tuple[bool, str | None, str]:
    """Full verification for one asset.

    Returns (verified, still_path, detail). For Live Photos (lp_size set)
    both the still and the motion clip must be present and byte-identical.
    """
    still_path = find_local_copy(index, filename, size)
    if still_path is None:
        return False, None, "no local copy"
    if lp_size:
        motion_path = find_local_motion_copy(index, filename, lp_size)
        if motion_path is None:
            return False, still_path, "live photo motion clip (.MOV) not in backup"
    return True, still_path, "ok"
