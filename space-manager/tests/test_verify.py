import os

from app.verify import build_index, find_local_copy


def make(tmp_path, rel, size):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


def test_exact_name_and_size_matches(tmp_path):
    p = make(tmp_path, "2024/06/01/IMG_1234.MOV", 1000)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) == p


def test_size_mismatch_is_not_verified(tmp_path):
    make(tmp_path, "IMG_1234.MOV", 999)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) is None


def test_name_mismatch_with_same_size_is_not_verified(tmp_path):
    make(tmp_path, "IMG_9999.MOV", 1000)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) is None


def test_dedup_suffix_variant_matches(tmp_path):
    # icloudpd name-size-dedup-with-suffix policy: IMG_1234-<bytes>.MOV
    p = make(tmp_path, "IMG_1234-1000.MOV", 1000)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) == p


def test_legacy_original_suffix_matches(tmp_path):
    p = make(tmp_path, "IMG_1234-original.MOV", 1000)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) == p


def test_case_insensitive_match(tmp_path):
    p = make(tmp_path, "img_1234.mov", 1000)
    index = build_index([str(tmp_path)])
    assert find_local_copy(index, "IMG_1234.MOV", 1000) == p


def test_missing_backup_dir_reports_error(tmp_path):
    index = build_index([str(tmp_path / "nope")])
    assert index.errors
    assert index.file_count == 0


def test_multiple_roots(tmp_path):
    make(tmp_path, "a/IMG_1.JPG", 10)
    p2 = make(tmp_path, "b/VID_2.MOV", 20)
    index = build_index([str(tmp_path / "a"), str(tmp_path / "b")])
    assert index.file_count == 2
    assert find_local_copy(index, "VID_2.MOV", 20) == p2
