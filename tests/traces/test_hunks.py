from git import Repo
from plumb.traces.hunks import parse_unified_hunks, staged_hunks, file_refs_for

DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10,0 +11,3 @@ def f():
+x
+y
+z
@@ -40,2 +44 @@
-old
-old2
+new
diff --git a/src/gone.py b/src/gone.py
--- a/src/gone.py
+++ /dev/null
@@ -1,5 +0,0 @@
-bye
"""


def test_parse_unified_hunks():
    assert parse_unified_hunks(DIFF) == {"src/a.py": [[11, 13], [44, 44]]}


def test_staged_hunks_real_repo(tmp_repo):
    (tmp_repo / "README.md").write_text("# Test Repo\nline2\nline3\n")
    repo = Repo(tmp_repo)
    repo.index.add(["README.md"])
    assert staged_hunks(repo) == {"README.md": [[2, 3]]}


def test_file_refs_for_intersects_edited_paths():
    hunks = {"src/a.py": [[11, 13]], "src/b.py": [[1, 1]]}
    refs = file_refs_for(["src/a.py", "src/untouched.py", "/abs/src/b.py"], hunks, repo_root="/abs")
    assert [(r.file, r.lines) for r in refs] == [("src/a.py", [11, 13]), ("src/b.py", [1, 1])]


def _commit_all(repo, msg="c"):
    repo.index.commit(msg)


def test_staged_hunks_ignores_injected_diff_headers_in_content(tmp_repo):
    # Content lines like "++ b/x.py" render as "+++ b/x.py" in -U0 output and
    # must not hijack the current file; "++ /dev/null" must not drop later hunks.
    repo = Repo(tmp_repo)
    (tmp_repo / "real.py").write_text("a\nb\nc\nd\ne\nf\n")
    repo.index.add(["real.py"])
    _commit_all(repo)
    (tmp_repo / "real.py").write_text("a\n++ b/injected.py\n++ /dev/null\nc\nd\ne\nZ\n")
    repo.index.add(["real.py"])
    hunks = staged_hunks(repo)
    assert "injected.py" not in hunks
    assert hunks == {"real.py": [[2, 3], [7, 7]]}


def test_staged_hunks_path_with_space_has_no_trailing_tab(tmp_repo):
    repo = Repo(tmp_repo)
    (tmp_repo / "dir sp").mkdir()
    (tmp_repo / "dir sp" / "f.py").write_text("x\n")
    repo.index.add(["dir sp/f.py"])
    assert staged_hunks(repo) == {"dir sp/f.py": [[1, 1]]}


def test_staged_hunks_non_ascii_path_not_escaped(tmp_repo):
    repo = Repo(tmp_repo)
    (tmp_repo / "café.py").write_text("x\n")
    repo.index.add(["café.py"])
    assert staged_hunks(repo) == {"café.py": [[1, 1]]}


def test_file_refs_for_normalizes_relative_paths():
    hunks = {"src/a.py": [[11, 13]]}
    refs = file_refs_for(["./src/a.py", "src/../src/a.py"], hunks, repo_root="/abs")
    assert [(r.file, r.lines) for r in refs] == [("src/a.py", [11, 13])]


def test_file_refs_for_resolves_relative_against_cwd():
    hunks = {"pkg/a.py": [[1, 1]]}
    refs = file_refs_for(["a.py"], hunks, repo_root="/abs", cwd="/abs/pkg")
    assert [(r.file, r.lines) for r in refs] == [("pkg/a.py", [1, 1])]


def test_file_refs_for_drops_paths_when_cwd_outside_repo():
    hunks = {"a.py": [[1, 1]]}
    assert file_refs_for(["a.py"], hunks, repo_root="/abs", cwd="/elsewhere") == []
