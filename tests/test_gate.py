import json

from plumb.config import ensure_plumb_dir
from plumb.gate import (
    EMPTY_SIGNATURE,
    clear_gate_state,
    diff_signature,
    read_gate_state,
    write_gate_state,
)

SAMPLE_DIFF = """\
diff --git a/app.py b/app.py
index e69de29..7898192 100644
--- a/app.py
+++ b/app.py
@@ -0,0 +1 @@
+x = 1
"""

OTHER_DIFF = """\
diff --git a/other.py b/other.py
index e69de29..7898192 100644
--- a/other.py
+++ b/other.py
@@ -0,0 +1 @@
+y = 2
"""


class TestDiffSignature:
    def test_stable_for_same_diff(self, tmp_repo):
        first = diff_signature(tmp_repo, SAMPLE_DIFF)
        second = diff_signature(tmp_repo, SAMPLE_DIFF)
        assert first == second
        assert first != EMPTY_SIGNATURE

    def test_differs_for_different_diff(self, tmp_repo):
        assert diff_signature(tmp_repo, SAMPLE_DIFF) != diff_signature(tmp_repo, OTHER_DIFF)

    def test_empty_diff(self, tmp_repo):
        assert diff_signature(tmp_repo, "") == EMPTY_SIGNATURE
        assert diff_signature(tmp_repo, "\n") == EMPTY_SIGNATURE


class TestGateState:
    def test_read_missing_returns_none(self, tmp_repo):
        assert read_gate_state(tmp_repo) is None

    def test_read_corrupt_returns_none(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        (tmp_repo / ".plumb" / "gate.json").write_text("not json{{{")
        assert read_gate_state(tmp_repo) is None

    def test_write_read_roundtrip(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        write_gate_state(tmp_repo, "abc123")
        state = read_gate_state(tmp_repo)
        assert state["diff_signature"] == "abc123"
        assert state["created_at"]

    def test_clear_removes_file(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        write_gate_state(tmp_repo, "abc123")
        clear_gate_state(tmp_repo)
        assert not (tmp_repo / ".plumb" / "gate.json").exists()

    def test_clear_noop_when_absent(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        clear_gate_state(tmp_repo)  # must not raise
        assert read_gate_state(tmp_repo) is None


class TestEnsurePlumbDirGitignore:
    def test_fresh_dir_writes_all_lines(self, tmp_path):
        ensure_plumb_dir(tmp_path)
        gi = tmp_path / ".plumb" / ".gitignore"
        assert gi.read_text() == "record.log\nrecord.lock\ngate.json\n"

    def test_appends_missing_line_to_old_file(self, tmp_path):
        (tmp_path / ".plumb").mkdir()
        gi = tmp_path / ".plumb" / ".gitignore"
        gi.write_text("record.log\nrecord.lock\n")
        ensure_plumb_dir(tmp_path)
        assert gi.read_text() == "record.log\nrecord.lock\ngate.json\n"

    def test_preserves_custom_lines(self, tmp_path):
        (tmp_path / ".plumb").mkdir()
        gi = tmp_path / ".plumb" / ".gitignore"
        gi.write_text("custom\n")
        ensure_plumb_dir(tmp_path)
        assert gi.read_text() == "custom\nrecord.log\nrecord.lock\ngate.json\n"

    def test_current_file_untouched(self, tmp_path):
        ensure_plumb_dir(tmp_path)
        gi = tmp_path / ".plumb" / ".gitignore"
        before = gi.read_text()
        mtime = gi.stat().st_mtime_ns
        ensure_plumb_dir(tmp_path)
        assert gi.read_text() == before
        assert gi.stat().st_mtime_ns == mtime
