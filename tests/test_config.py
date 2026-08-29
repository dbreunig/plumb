import json
from pathlib import Path

from plumb.config import (
    PlumbConfig,
    find_repo_root,
    ensure_plumb_dir,
    load_config,
    save_config,
)


class TestPlumbConfig:
    def test_default_values(self):
        cfg = PlumbConfig()
        assert cfg.spec_paths == []
        assert cfg.test_paths == []
        assert cfg.last_commit is None
        assert cfg.program_models == {}

    def test_custom_values(self):
        cfg = PlumbConfig(
            spec_paths=["docs/spec.md"],
            test_paths=["tests/"],
        )
        assert cfg.spec_paths == ["docs/spec.md"]
        assert cfg.test_paths == ["tests/"]

    def test_serialization_roundtrip(self):
        cfg = PlumbConfig(spec_paths=["a.md"], test_paths=["t/"])
        data = cfg.model_dump()
        cfg2 = PlumbConfig(**data)
        assert cfg == cfg2

    def test_program_models_roundtrip(self):
        cfg = PlumbConfig(
            spec_paths=["a.md"],
            program_models={
                "decision_deduplicator": {"model": "groq/openai/gpt-oss-120b", "max_tokens": 8192},
            },
        )
        data = cfg.model_dump()
        cfg2 = PlumbConfig(**data)
        assert cfg2.program_models["decision_deduplicator"]["model"] == "groq/openai/gpt-oss-120b"
        assert cfg2.program_models["decision_deduplicator"]["max_tokens"] == 8192


class TestFindRepoRoot:
    def test_finds_git_repo(self, tmp_repo):
        # plumb:req-fedab03e
        assert find_repo_root(tmp_repo) == tmp_repo

    def test_finds_from_subdirectory(self, tmp_repo):
        sub = tmp_repo / "subdir"
        sub.mkdir()
        assert find_repo_root(sub) == tmp_repo

    def test_returns_none_for_non_repo(self, tmp_path):
        # plumb:req-dc5b8f48
        assert find_repo_root(tmp_path) is None


class TestEnsurePlumbDir:
    def test_creates_directory(self, tmp_repo):
        # plumb:req-27edd42d
        plumb_dir = ensure_plumb_dir(tmp_repo)
        assert plumb_dir.exists()
        assert plumb_dir.name == ".plumb"

    def test_idempotent(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        ensure_plumb_dir(tmp_repo)
        assert (tmp_repo / ".plumb").exists()


class TestLoadSaveConfig:
    def test_save_and_load(self, tmp_repo):
        # plumb:req-1a094799
        # plumb:req-87c1d58b
        ensure_plumb_dir(tmp_repo)
        cfg = PlumbConfig(spec_paths=["spec.md"], test_paths=["tests/"])
        save_config(tmp_repo, cfg)
        loaded = load_config(tmp_repo)
        assert loaded is not None
        assert loaded.spec_paths == ["spec.md"]

    def test_load_missing(self, tmp_repo):
        assert load_config(tmp_repo) is None

    def test_load_malformed(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        cp = tmp_repo / ".plumb" / "config.json"
        cp.write_text("not json{{{")
        assert load_config(tmp_repo) is None

    def test_atomic_write(self, tmp_repo):
        ensure_plumb_dir(tmp_repo)
        cfg = PlumbConfig(spec_paths=["a.md"])
        save_config(tmp_repo, cfg)
        # File should exist and be valid JSON
        cp = tmp_repo / ".plumb" / "config.json"
        data = json.loads(cp.read_text())
        assert data["spec_paths"] == ["a.md"]


def test_mode_defaults_and_validation():
    import pytest
    from plumb.config import PlumbConfig, effective_mode
    cfg = PlumbConfig()
    assert cfg.mode == "review" and cfg.record_threshold is None
    assert effective_mode(cfg) == ("review", "config")
    assert effective_mode(None) == ("review", "default")
    assert effective_mode(PlumbConfig(mode="record")) == ("record", "config")
    with pytest.raises(ValueError):
        PlumbConfig(mode="gate")
    with pytest.raises(ValueError):
        PlumbConfig(record_threshold=1.5)
    assert PlumbConfig(record_threshold=0.7).record_threshold == 0.7


def test_plumb_mode_env_overrides_config(monkeypatch):
    from plumb.config import PlumbConfig, effective_mode
    monkeypatch.setenv("PLUMB_MODE", "Record ")
    assert effective_mode(PlumbConfig(mode="review")) == ("record", "env")
    monkeypatch.setenv("PLUMB_MODE", "bogus")
    assert effective_mode(PlumbConfig(mode="review")) == ("review", "config")
    monkeypatch.setenv("PLUMB_MODE", "")
    assert effective_mode(PlumbConfig(mode="record")) == ("record", "config")


def test_old_config_without_mode_loads(tmp_repo):
    import json
    from plumb.config import PlumbConfig, save_config, load_config
    save_config(tmp_repo, PlumbConfig(spec_paths=["s.md"]))
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text()); data.pop("mode"); data.pop("record_threshold")
    p.write_text(json.dumps(data))
    cfg = load_config(tmp_repo)
    assert cfg is not None and cfg.mode == "review" and cfg.record_threshold is None
