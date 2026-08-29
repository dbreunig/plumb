"""Record mode: post-commit extraction for a landed commit (plumb/record.py)."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from git import Repo

from plumb.config import load_config, save_config
from plumb.decision_log import Decision, append_decisions, read_all_decisions


def _mock(conf, id_="dec-m"):
    return Decision(
        id=id_, status="pending", question="Q?", decision="A.", made_by="agent",
        confidence=conf, branch="main", created_at=datetime.now(timezone.utc).isoformat(),
    )


def _commit(repo_root, name="a.py", text="x = 1\n", msg="c"):
    (repo_root / name).write_text(text)
    r = Repo(repo_root)
    r.index.add([name])
    return r.index.commit(msg).hexsha


def test_record_extract_writes_recorded_with_commit_sha(initialized_repo):
    from plumb.record import record_extract
    cfg = load_config(initialized_repo)
    cfg.mode = "record"
    save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    with patch("plumb.git_hook.extract_decisions", return_value=[_mock(0.9)]):
        written = record_extract(initialized_repo, sha)
    assert len(written) == 1
    d = read_all_decisions(initialized_repo)[0]
    assert (d.status, d.approved_by, d.commit_sha) == ("recorded", "auto", sha)


def test_record_extract_threshold_splits(initialized_repo):
    from plumb.record import record_extract
    cfg = load_config(initialized_repo)
    cfg.mode = "record"
    cfg.record_threshold = 0.8
    save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    with patch("plumb.git_hook.extract_decisions", return_value=[_mock(0.9, "hi"), _mock(0.5, "lo")]):
        record_extract(initialized_repo, sha)
    by = {d.id: d for d in read_all_decisions(initialized_repo)}
    assert by["hi"].status == "recorded" and by["hi"].commit_sha == sha
    assert by["lo"].status == "pending" and by["lo"].commit_sha == sha and by["lo"].approved_by is None


def test_record_extract_uses_commit_diff_not_staged(initialized_repo):
    from plumb.record import record_extract
    parent = Repo(initialized_repo).head.commit
    sha = _commit(initialized_repo, text="committed = 1\n")
    # Unrelated change sitting in the index must not leak into the commit's extraction.
    (initialized_repo / "b.py").write_text("staged_only = 1\n")
    Repo(initialized_repo).index.add(["b.py"])
    seen = {}

    def fake(repo_root, config, diff, branch, **kw):
        seen["diff"] = diff
        seen.update(kw)
        return []

    with patch("plumb.git_hook.extract_decisions", side_effect=fake):
        record_extract(initialized_repo, sha)
    assert "committed = 1" in seen["diff"] and "staged_only" not in seen["diff"]
    # file_refs are derived from the hunks handed to extract_decisions: the
    # commit's own line ranges, not the index's.
    assert seen["hunks"] == {"a.py": [[1, 1]]}
    # Transcripts are read since the previous commit (explicit datetime, so
    # nothing falls back to the config cutoff).
    assert seen["since_commit"] is None
    assert seen["since_datetime"] == parent.committed_datetime.isoformat()


def test_record_extract_skips_ignored_only_commits(initialized_repo):
    from plumb.record import record_extract
    # README.md is ignored by the default .plumbignore (no file in the fixture).
    sha = _commit(initialized_repo, name="README.md", text="# Test Repo\n\nMore.\n")
    with patch("plumb.git_hook.extract_decisions") as ex:
        assert record_extract(initialized_repo, sha) == []
    ex.assert_not_called()
    assert read_all_decisions(initialized_repo) == []


def test_record_extract_amend_deletes_replaced_commits_decisions(initialized_repo):
    from plumb.record import record_extract
    sha1 = _commit(initialized_repo)
    cfg = load_config(initialized_repo)
    cfg.last_commit = sha1
    save_config(initialized_repo, cfg)
    append_decisions(
        initialized_repo,
        [Decision(id="dec-old", status="recorded", decision="old", commit_sha=sha1, branch="main")],
        branch="main",
    )
    r = Repo(initialized_repo)
    (initialized_repo / "a.py").write_text("x = 2\n")
    r.index.add(["a.py"])
    # Simulate `commit --amend`: a new commit with the same parent as sha1.
    parents = [r.head.commit.parents[0]] if r.head.commit.parents else []
    sha2 = r.index.commit("c", parent_commits=parents).hexsha
    with patch("plumb.git_hook.extract_decisions", return_value=[]):
        record_extract(initialized_repo, sha2)
    assert all(d.id != "dec-old" for d in read_all_decisions(initialized_repo))


def test_run_hook_is_noop_in_record_mode(initialized_repo):
    from plumb.git_hook import run_hook
    cfg = load_config(initialized_repo)
    cfg.mode = "record"
    save_config(initialized_repo, cfg)
    (initialized_repo / "z.py").write_text("z = 1\n")
    Repo(initialized_repo).index.add(["z.py"])
    with patch("plumb.git_hook.extract_decisions") as ex:
        assert run_hook(initialized_repo) == 0
    ex.assert_not_called()


def test_post_commit_spawns_worker_in_record_mode(initialized_repo, monkeypatch):
    from plumb.git_hook import run_post_commit
    cfg = load_config(initialized_repo)
    cfg.mode = "record"
    save_config(initialized_repo, cfg)
    sha = _commit(initialized_repo)
    spawned = {}
    monkeypatch.setattr("plumb.record.spawn_worker", lambda repo_root, s, b: spawned.update(sha=s, branch=b))
    run_post_commit(initialized_repo)
    assert spawned["sha"] == sha
    assert spawned["branch"] == "main"
    assert load_config(initialized_repo).last_commit == sha


def test_post_commit_does_not_spawn_worker_in_review_mode(initialized_repo, monkeypatch):
    from plumb.git_hook import run_post_commit
    _commit(initialized_repo)
    spawned = {}
    monkeypatch.setattr("plumb.record.spawn_worker", lambda repo_root, s, b: spawned.update(sha=s))
    run_post_commit(initialized_repo)
    assert spawned == {}


def test_worker_lock_serializes(initialized_repo):
    from plumb.record import record_lock
    with record_lock(initialized_repo) as got:
        assert got is True
        with record_lock(initialized_repo, wait=False) as got2:
            assert got2 is False


# --- review fixes ---------------------------------------------------------


def test_plumb_diff_still_extracts_in_record_mode(initialized_repo):
    """`plumb diff` (dry run) is read-only, so record mode must not short-circuit it."""
    from plumb.git_hook import run_hook
    cfg = load_config(initialized_repo)
    cfg.mode = "record"
    save_config(initialized_repo, cfg)
    (initialized_repo / "z.py").write_text("z = 1\n")
    Repo(initialized_repo).index.add(["z.py"])
    with patch("plumb.git_hook.extract_decisions", return_value=[]) as ex:
        assert run_hook(initialized_repo, dry_run=True) == 0
    ex.assert_called_once()
    assert read_all_decisions(initialized_repo) == []


def test_record_extract_writes_to_explicit_branch_shard(initialized_repo):
    from plumb.decision_log import read_decisions
    from plumb.record import record_extract
    sha = _commit(initialized_repo)
    Repo(initialized_repo).git.checkout("-b", "side")
    with patch("plumb.git_hook.extract_decisions", return_value=[_mock(0.9)]):
        written = record_extract(initialized_repo, sha, branch="main")
    assert len(written) == 1 and written[0].branch == "main"
    assert [d.id for d in read_decisions(initialized_repo, branch="main")] == ["dec-m"]
    assert read_decisions(initialized_repo, branch="side") == []


def test_record_extract_skips_commit_not_on_any_ref(initialized_repo):
    from plumb.record import record_extract
    sha1 = _commit(initialized_repo)
    r = Repo(initialized_repo)
    (initialized_repo / "a.py").write_text("x = 2\n")
    r.index.add(["a.py"])
    r.index.commit("amended", parent_commits=[r.head.commit.parents[0]])  # main now skips sha1
    assert r.git.branch("--all", "--contains", sha1).strip() == ""
    with patch("plumb.git_hook.extract_decisions") as ex:
        assert record_extract(initialized_repo, sha1) == []
    ex.assert_not_called()


def test_amend_detection_keeps_predecessor_still_on_a_branch(initialized_repo):
    """A same-parent sibling is not an amend if the old commit is still on some ref."""
    from plumb.record import record_extract
    sha1 = _commit(initialized_repo)
    r = Repo(initialized_repo)
    r.git.branch("keep", sha1)
    cfg = load_config(initialized_repo)
    cfg.last_commit = sha1
    save_config(initialized_repo, cfg)
    append_decisions(
        initialized_repo,
        [Decision(id="dec-old", status="recorded", decision="old", commit_sha=sha1, branch="main")],
        branch="main",
    )
    (initialized_repo / "a.py").write_text("x = 2\n")
    r.index.add(["a.py"])
    sha2 = r.index.commit("sibling", parent_commits=[r.head.commit.parents[0]]).hexsha
    with patch("plumb.git_hook.extract_decisions", return_value=[]):
        record_extract(initialized_repo, sha2)
    assert any(d.id == "dec-old" for d in read_all_decisions(initialized_repo))


def test_spawn_worker_detached_argv(initialized_repo):
    import subprocess
    import sys
    from plumb.record import spawn_worker
    with patch("plumb.record.subprocess.Popen") as popen:
        spawn_worker(initialized_repo, "abc123", "main")
    (argv,), kw = popen.call_args
    assert argv == [sys.executable, "-m", "plumb.cli", "record-extract", "abc123", "--branch", "main"]
    assert kw["cwd"] == str(initialized_repo)
    assert kw["start_new_session"] is True
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stderr"] is subprocess.STDOUT
    assert Path(kw["stdout"].name) == initialized_repo / ".plumb" / "record.log"


def test_record_extract_cutoff_is_parent_datetime_not_config(initialized_repo):
    from plumb.record import record_extract
    parent = Repo(initialized_repo).head.commit
    sha = _commit(initialized_repo)
    cfg = load_config(initialized_repo)
    cfg.last_commit = sha  # post-commit already advanced the cutoff to this commit
    save_config(initialized_repo, cfg)
    seen = {}

    def fake(repo_root, config, diff, branch, **kw):
        seen.update(kw)
        return []

    with patch("plumb.git_hook.extract_decisions", side_effect=fake):
        record_extract(initialized_repo, sha)
    assert seen["since_commit"] is None
    assert seen["since_datetime"] == parent.committed_datetime.isoformat()


def test_record_extract_root_commit_uses_epoch_cutoff(tmp_path):
    from plumb.config import PlumbConfig, ensure_plumb_dir
    from plumb.record import record_extract
    root = tmp_path / "fresh"
    Repo.init(root)
    ensure_plumb_dir(root)
    save_config(root, PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"]))
    sha = _commit(root)
    cfg = load_config(root)
    cfg.last_commit = sha
    save_config(root, cfg)
    seen = {}

    def fake(repo_root, config, diff, branch, **kw):
        seen.update(kw)
        return []

    with patch("plumb.git_hook.extract_decisions", side_effect=fake):
        record_extract(root, sha)
    assert seen["since_commit"] is None
    assert seen["since_datetime"] == "1970-01-01T00:00:00+00:00"


def test_status_in_review_mode_leaves_no_lock_file(initialized_repo, monkeypatch):
    from click.testing import CliRunner
    from plumb.cli import cli
    monkeypatch.chdir(initialized_repo)
    r = CliRunner().invoke(cli, ["status"])
    assert r.exit_code == 0, r.output
    assert not (initialized_repo / ".plumb" / "record.lock").exists()
