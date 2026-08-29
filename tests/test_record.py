"""Record mode: post-commit extraction for a landed commit (plumb/record.py)."""
from datetime import datetime, timezone
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
    parent = Repo(initialized_repo).head.commit.hexsha
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
    # Transcripts are read since the previous commit, not the config cutoff.
    assert seen["since_commit"] == parent
    assert seen["since_datetime"] is None


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
    monkeypatch.setattr("plumb.record.spawn_worker", lambda repo_root, s: spawned.update(sha=s))
    run_post_commit(initialized_repo)
    assert spawned["sha"] == sha
    assert load_config(initialized_repo).last_commit == sha


def test_post_commit_does_not_spawn_worker_in_review_mode(initialized_repo, monkeypatch):
    from plumb.git_hook import run_post_commit
    _commit(initialized_repo)
    spawned = {}
    monkeypatch.setattr("plumb.record.spawn_worker", lambda repo_root, s: spawned.update(sha=s))
    run_post_commit(initialized_repo)
    assert spawned == {}


def test_worker_lock_serializes(initialized_repo):
    from plumb.record import record_lock
    with record_lock(initialized_repo) as got:
        assert got is True
        with record_lock(initialized_repo, wait=False) as got2:
            assert got2 is False
