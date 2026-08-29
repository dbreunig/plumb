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
