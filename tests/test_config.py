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


def test_invalid_mode_in_file_falls_back_with_warning(tmp_repo, capsys):
    import json
    from plumb.config import PlumbConfig, save_config, load_config
    save_config(tmp_repo, PlumbConfig(spec_paths=["s.md"], last_commit="abc"))
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text()); data["mode"] = "gate"; data["record_threshold"] = 1.5
    p.write_text(json.dumps(data))
    cfg = load_config(tmp_repo)
    assert cfg is not None and cfg.mode == "review" and cfg.record_threshold is None
    assert cfg.last_commit == "abc"                     # nothing else lost
    err = capsys.readouterr().err
    assert "mode" in err and "gate" in err and "record_threshold" in err


def test_other_validation_errors_still_return_none(tmp_repo):
    from plumb.config import load_config
    p = tmp_repo / ".plumb"; p.mkdir(exist_ok=True)
    (p / "config.json").write_text('{"spec_paths": "not-a-list"}')
    assert load_config(tmp_repo) is None


def test_ensure_plumb_dir_ignores_record_runtime_files(tmp_path):
    ensure_plumb_dir(tmp_path)
    gi = tmp_path / ".plumb" / ".gitignore"
    assert gi.read_text() == "record.log\nrecord.lock\n"
    gi.write_text("custom\n")
    ensure_plumb_dir(tmp_path)  # idempotent: never clobbers an existing file
    assert gi.read_text() == "custom\n"


def test_model_field_defaults_and_roundtrip(tmp_repo):
    from plumb.config import DEFAULT_MODEL, PlumbConfig, save_config, load_config
    assert DEFAULT_MODEL == "anthropic/claude-haiku-4-5"
    assert PlumbConfig().model == DEFAULT_MODEL
    save_config(tmp_repo, PlumbConfig(model="groq/llama-3.3-70b-versatile"))
    assert load_config(tmp_repo).model == "groq/llama-3.3-70b-versatile"
    # old configs without the field load with the default
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text())
    data.pop("model")
    p.write_text(json.dumps(data))
    assert load_config(tmp_repo).model == DEFAULT_MODEL


def test_invalid_model_in_file_falls_back_with_warning(tmp_repo, capsys):
    """A hand-edited empty model warns and falls back instead of disabling Plumb."""
    from plumb.config import DEFAULT_MODEL, PlumbConfig, save_config, load_config
    save_config(tmp_repo, PlumbConfig(spec_paths=["s.md"], last_commit="abc"))
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text())
    data["model"] = "   "
    p.write_text(json.dumps(data))
    cfg = load_config(tmp_repo)
    assert cfg is not None and cfg.model == DEFAULT_MODEL
    assert cfg.last_commit == "abc"                     # nothing else lost
    err = capsys.readouterr().err
    assert "model" in err and "litellm model string" in err


def test_lenient_warning_printed_once_per_process(tmp_repo, capsys):
    """Repeated load_config calls warn once, not once per call (plumb status
    loads the config three times)."""
    from plumb.config import PlumbConfig, save_config, load_config
    save_config(tmp_repo, PlumbConfig(spec_paths=["s.md"]))
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text())
    data["model"] = ""
    p.write_text(json.dumps(data))
    for _ in range(3):
        cfg = load_config(tmp_repo)
        assert cfg is not None
    err = capsys.readouterr().err
    assert err.count("plumb: warning") == 1
