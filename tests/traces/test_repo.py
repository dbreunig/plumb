import subprocess
from plumb.traces.repo import git_common_dir, same_repo, commit_datetime


def test_git_common_dir_resolves_subdir(tmp_repo):
    sub = tmp_repo / "pkg"
    sub.mkdir()
    assert git_common_dir(str(sub)) == (tmp_repo / ".git").resolve()


def test_git_common_dir_none_outside_repo(tmp_path):
    assert git_common_dir(str(tmp_path)) is None


def test_same_repo_true_for_subdir(tmp_repo):
    assert same_repo(str(tmp_repo / "pkg"), tmp_repo)


def test_same_repo_true_for_linked_worktree(tmp_repo, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(tmp_repo), "worktree", "add", "--detach", str(wt)],
                   check=True, capture_output=True)
    assert same_repo(str(wt), tmp_repo)


def test_same_repo_false_for_other_repo(tmp_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert not same_repo(str(other), tmp_repo)


def test_same_repo_false_for_missing_path(tmp_repo):
    assert not same_repo("/definitely/not/here", tmp_repo)


def test_commit_datetime(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    assert commit_datetime(tmp_repo, sha) is not None
    assert commit_datetime(tmp_repo, "0" * 40) is None


from datetime import datetime, timezone
from plumb.traces.repo import resolve_cutoff, parse_ts, after_cutoff


def test_resolve_cutoff_prefers_datetime(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    dt = resolve_cutoff(tmp_repo, since_commit=sha, since_datetime="2030-01-01T00:00:00Z")
    assert dt.year == 2030


def test_resolve_cutoff_falls_back_to_commit(tmp_repo):
    from git import Repo
    sha = Repo(tmp_repo).head.commit.hexsha
    assert resolve_cutoff(tmp_repo, since_commit=sha, since_datetime=None) is not None


def test_resolve_cutoff_none(tmp_repo):
    assert resolve_cutoff(tmp_repo, None, None) is None


def test_parse_ts_handles_z_and_epoch_ms():
    assert parse_ts("2026-01-01T00:00:00Z").tzinfo is not None
    assert parse_ts(1786916035750).year == 2026
    assert parse_ts("garbage") is None
    assert parse_ts(None) is None


def test_after_cutoff():
    cut = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert after_cutoff("2026-01-02T00:00:00Z", cut)
    assert not after_cutoff("2025-12-31T00:00:00Z", cut)
    assert after_cutoff(None, cut)          # unknown timestamps are kept
    assert after_cutoff("2025-01-01T00:00:00Z", None)
