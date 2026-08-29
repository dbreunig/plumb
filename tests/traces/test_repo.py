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
