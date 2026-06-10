"""Unit tests for ``patch_target_paths`` — the unified-diff path parser used to verify
``git apply`` actually changed the build tree (issue #227)."""

from __future__ import annotations

from pathlib import Path

from kdive.providers.build_validation import patch_target_paths

_MODIFY = "--- a/fs/dcache.c\n+++ b/fs/dcache.c\n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2-patched\n"


def test_parses_modified_file_with_p1_strip() -> None:
    assert patch_target_paths(_MODIFY, strip=1) == {Path("fs/dcache.c")}


def test_new_file_ignores_dev_null_source() -> None:
    patch = "--- /dev/null\n+++ b/init/new.c\n@@ -0,0 +1 @@\n+hello\n"
    assert patch_target_paths(patch, strip=1) == {Path("init/new.c")}


def test_deleted_file_ignores_dev_null_target() -> None:
    patch = "--- a/init/old.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    assert patch_target_paths(patch, strip=1) == {Path("init/old.c")}


def test_multiple_files() -> None:
    patch = _MODIFY + "--- a/kernel/sched.c\n+++ b/kernel/sched.c\n@@ -1 +1 @@\n-a\n+b\n"
    assert patch_target_paths(patch, strip=1) == {
        Path("fs/dcache.c"),
        Path("kernel/sched.c"),
    }


def test_strips_trailing_timestamp_after_tab() -> None:
    patch = "--- a/fs/dcache.c\t2026-06-10 00:00:00\n+++ b/fs/dcache.c\t2026-06-10 00:00:01\n"
    assert patch_target_paths(patch, strip=1) == {Path("fs/dcache.c")}


def test_path_shallower_than_strip_is_dropped() -> None:
    # "+++ toplevel" has only one component; -p1 strips it, leaving nothing to target.
    assert patch_target_paths("--- toplevel\n+++ toplevel\n", strip=1) == set()


def test_empty_patch_has_no_targets() -> None:
    assert patch_target_paths("", strip=1) == set()


def test_git_quoted_path_is_skipped() -> None:
    # git c-quotes paths with special/non-ASCII bytes; they can't be reliably -p stripped,
    # so they are excluded (the caller's git-apply stderr check covers correctness instead).
    patch = '--- "a/fs/\\303\\251.c"\n+++ "b/fs/\\303\\251.c"\n'
    assert patch_target_paths(patch, strip=1) == set()
