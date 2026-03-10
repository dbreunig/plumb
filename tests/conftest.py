import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git import Repo

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb.decision_log import Decision, generate_decision_id


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = Repo.init(tmp_path)
    # Create an initial file and commit
    readme = tmp_path / "README.md"
    readme.write_text("# Test Repo\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return tmp_path


@pytest.fixture
def initialized_repo(tmp_repo):
    """A tmp_repo with .plumb/ directory and config."""
    ensure_plumb_dir(tmp_repo)
    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
    save_config(tmp_repo, cfg)
    # Create a spec file
    spec = tmp_repo / "spec.md"
    spec.write_text("# Spec\n\n## Features\n\nThe system must do X.\n")
    # Create tests dir
    (tmp_repo / "tests").mkdir(exist_ok=True)
    (tmp_repo / ".plumb" / "decisions").mkdir(exist_ok=True)
    return tmp_repo


@pytest.fixture
def initialized_repo_dir_specs(tmp_repo):
    """A tmp_repo with spec_paths pointing to a directory (not individual files)."""
    ensure_plumb_dir(tmp_repo)
    cfg = PlumbConfig(
        spec_paths=["specs/"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
    save_config(tmp_repo, cfg)
    # Create specs directory with multiple spec files
    specs_dir = tmp_repo / "specs"
    specs_dir.mkdir(exist_ok=True)
    (specs_dir / "spec.md").write_text("# Spec\n\n## Features\n\nThe system must do X.\n")
    (specs_dir / "api.md").write_text("# API\n\n## Endpoints\n\nGET /items returns all items.\n")
    # Create tests dir
    (tmp_repo / "tests").mkdir(exist_ok=True)
    (tmp_repo / ".plumb" / "decisions").mkdir(exist_ok=True)
    return tmp_repo


@pytest.fixture
def sample_decisions():
    """Return a list of sample Decision objects."""
    return [
        Decision(
            id="dec-aaa111",
            status="pending",
            question="Should we use sync or async?",
            decision="Use sync for simplicity.",
            made_by="user",
            branch="main",
            confidence=0.9,
            chunk_index=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
        Decision(
            id="dec-bbb222",
            status="pending",
            question="Cache in memory or on disk?",
            decision="In-memory dict cache.",
            made_by="llm",
            branch="main",
            confidence=0.85,
            chunk_index=1,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    ]


@pytest.fixture
def sample_config():
    return PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        initialized_at=datetime.now(timezone.utc).isoformat(),
    )
