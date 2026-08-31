# Provider-Neutral Inference Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** The default inference model becomes a config field holding a litellm model string, every call site resolves through it, the key check is provider-aware, `plumb model` inspects and changes it (with a connectivity test before saving), and `plumb init` confirms the default with Y/n or walks through provider options.

**Architecture:** `PlumbConfig.model` (default `anthropic/claude-haiku-4-5`) feeds `get_lm(repo_root)`; `validate_api_access` uses `litellm.validate_environment` to name the exact missing env var before the smoke test; `code_modifier` moves from the raw `anthropic` SDK to `litellm.completion`; `_llm_dedup`'s fallback becomes `get_lm()`. Design: `docs/plans/2026-08-31-provider-neutral-inference-design.md`.

**Conventions for the executor:**
- @superpowers:test-driven-development — failing test first, every task.
- Baseline: `uv run pytest tests/ -q` → **743 passed, 10 skipped, 0 failed** (bare run; the 10 skips are the e2e suite, which needs `PLUMB_E2E=1`).
- Never run `plumb hook/diff/sync/record-extract` or the e2e against this repo without instruction — they call an LLM. Unit tests must patch above litellm (patch `plumb.programs.get_lm`, `litellm.validate_environment`, `litellm.completion`, or `dspy.LM` as appropriate) and never hit the network.
- This repo's own pre-commit hook is disabled. Commit normally with the exact messages; do NOT commit `.plumb/config.json`, `.plumb/code_coverage_map.json`, `dist/`, `uv.lock`.
- **Init gains a fourth prompt (Task 5).** Every existing scripted `plumb init` — the init tests in `tests/test_cli.py`, `tests/test_cli_extended.py`, `tests/test_integration.py`, and `tests/e2e/test_record_mode.py::world` — needs one more input line; an empty line accepts the default. Task 5 lists them.

---

## Task 1: `model` in config; `get_lm` resolves it; dedup fallback

**Files:** `plumb/config.py`, `plumb/programs/__init__.py`, `plumb/decision_log.py`; tests in `tests/test_config.py`, `tests/test_programs.py`, `tests/test_decision_log.py`.

**Step 1: failing tests**

```python
# tests/test_config.py
def test_model_field_defaults_and_roundtrip(tmp_repo):
    from plumb.config import DEFAULT_MODEL, PlumbConfig, save_config, load_config
    assert DEFAULT_MODEL == "anthropic/claude-haiku-4-5"
    assert PlumbConfig().model == DEFAULT_MODEL
    save_config(tmp_repo, PlumbConfig(model="groq/llama-3.3-70b-versatile"))
    assert load_config(tmp_repo).model == "groq/llama-3.3-70b-versatile"
    # old configs without the field load with the default
    import json
    p = tmp_repo / ".plumb" / "config.json"
    data = json.loads(p.read_text()); data.pop("model")
    p.write_text(json.dumps(data))
    assert load_config(tmp_repo).model == DEFAULT_MODEL
```

```python
# tests/test_programs.py
def test_get_lm_uses_configured_model(tmp_repo, monkeypatch):
    from plumb.config import PlumbConfig, save_config
    from plumb.programs import get_lm
    save_config(tmp_repo, PlumbConfig(model="openai/gpt-4.1-mini"))
    lm = get_lm(repo_root=tmp_repo)
    assert lm.model == "openai/gpt-4.1-mini"

def test_get_lm_falls_back_to_default_without_config(tmp_path, monkeypatch):
    from plumb.config import DEFAULT_MODEL
    from plumb.programs import get_lm
    monkeypatch.chdir(tmp_path)          # not a repo, no config
    assert get_lm().model == DEFAULT_MODEL
```

```python
# tests/test_decision_log.py
def test_llm_dedup_fallback_uses_get_lm(monkeypatch):
    """The dedup fallback LM comes from get_lm(), not a hardcoded model id."""
    from plumb.decision_log import Decision, _llm_dedup
    seen = {}
    class FakeLM:
        model = "fake/model"
    def fake_get_lm(repo_root=None):
        seen["called"] = True
        return FakeLM()
    class FakeDeduplicator:
        def __call__(self, candidates, existing): return []
    with patch("plumb.programs.get_lm", side_effect=fake_get_lm), \
         patch("plumb.programs.get_program_lm", return_value=None), \
         patch("plumb.programs.decision_deduplicator.DecisionDeduplicator", FakeDeduplicator), \
         patch("plumb.decision_log.dspy") as fake_dspy:
        fake_dspy.context.return_value.__enter__ = lambda s: None
        fake_dspy.context.return_value.__exit__ = lambda s, *a: False
        _llm_dedup([Decision(id="d1", decision="x")], [])
    assert seen.get("called")
```

Check how `_llm_dedup` imports `dspy` (module level `import dspy` inside the function) and adjust the patching to whatever actually intercepts; the assertion that matters is that `get_lm` was called and no `dspy.LM("anthropic/...")` literal remains (add a source assertion: `"anthropic/claude-haiku" not in inspect.getsource(_llm_dedup)`).

**Step 3: implement**
- `plumb/config.py`: `DEFAULT_MODEL = "anthropic/claude-haiku-4-5"`; `model: str = DEFAULT_MODEL` on `PlumbConfig` (a plain non-empty string; add a validator rejecting empty/whitespace and add `model` to `_LENIENT_FIELDS` with the hint "a litellm model string, e.g. anthropic/claude-haiku-4-5" so a hand-edited empty value warns and falls back instead of disabling Plumb).
- `plumb/programs/__init__.py`: `get_lm(repo_root: str | Path | None = None) -> dspy.LM` — resolve `find_repo_root()` when `None`, `load_config`, use `cfg.model` else `DEFAULT_MODEL`; keep `max_tokens=28000`. `configure_dspy(repo_root=None)` passes it through.
- `plumb/decision_log.py:532`: `lm = override_lm or get_lm()` (import from `plumb.programs` beside the existing `get_program_lm` import).

Commit: `feat(config): configurable inference model (litellm string); get_lm and dedup fallback resolve it`

---

## Task 2: provider-aware `validate_api_access`

**Files:** `plumb/programs/__init__.py`, `plumb/cli.py:158-163` (init failure guidance); tests in `tests/test_programs.py`.

**Step 1: failing tests**

```python
def test_validate_api_access_names_missing_provider_key(tmp_repo, monkeypatch):
    from plumb.config import PlumbConfig, save_config
    from plumb import PlumbAuthError
    from plumb.programs import validate_api_access
    save_config(tmp_repo, PlumbConfig(model="groq/llama-3.3-70b-versatile"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with patch("dotenv.load_dotenv"), \
         patch("litellm.validate_environment",
               return_value={"keys_in_environment": False, "missing_keys": ["GROQ_API_KEY"]}) as ve:
        with pytest.raises(PlumbAuthError, match="GROQ_API_KEY"):
            validate_api_access(repo_root=tmp_repo)
    ve.assert_called_once_with("groq/llama-3.3-70b-versatile")


def test_validate_api_access_smoke_tests_when_keys_present(tmp_repo, monkeypatch):
    from plumb.programs import validate_api_access
    mock_lm = MagicMock(return_value="hello")
    with patch("dotenv.load_dotenv"), \
         patch("litellm.validate_environment",
               return_value={"keys_in_environment": True, "missing_keys": []}), \
         patch("plumb.programs.get_lm", return_value=mock_lm):
        validate_api_access(repo_root=tmp_repo)
    mock_lm.assert_called_once()


def test_validate_api_access_keyless_provider_passes_env_check(tmp_repo):
    """A provider needing no key (e.g. ollama) goes straight to the smoke test."""
    from plumb.programs import validate_api_access
    mock_lm = MagicMock(return_value="hello")
    with patch("dotenv.load_dotenv"), \
         patch("litellm.validate_environment",
               return_value={"keys_in_environment": True, "missing_keys": []}), \
         patch("plumb.programs.get_lm", return_value=mock_lm):
        validate_api_access(repo_root=tmp_repo, model="ollama/llama3.1")
```

The existing `TestValidateApiAccess` tests assert `ANTHROPIC_API_KEY` messages; update them to the new behavior (for the default model, the missing key IS `ANTHROPIC_API_KEY`, so patch `litellm.validate_environment` accordingly and keep the assertions meaningful). Verify what `litellm.validate_environment` actually returns for a couple of models in a scratch line first (`uv run python -c "import litellm; print(litellm.validate_environment('groq/x'))"`) — no network is involved — and shape the code to the real return value; if the real shape differs from `{"keys_in_environment", "missing_keys"}`, follow reality and adjust the tests.

**Step 3: implement**
- `validate_api_access(repo_root=None, model=None)`: load dotenv; resolve model (arg → config → default); call `litellm.validate_environment(model)`; missing keys → `PlumbAuthError` naming them and the model ("GROQ_API_KEY is not set. Plumb is configured to use groq/llama-3.3-70b-versatile. Set it in .env or export it."); then smoke test via `get_lm(repo_root)` (or an LM built from the `model` arg when given). Keep the existing auth-vs-other error split.
- `cli.py` init failure guidance: replace the hardcoded `ANTHROPIC_API_KEY=sk-ant-...` lines with the message from the exception (print `e` and generic ".env or export" guidance).

Commit: `feat(auth): provider-aware key check names the exact missing env var`

---

## Task 3: `code_modifier` on litellm

**Files:** `plumb/programs/code_modifier.py`, `pyproject.toml`; tests in `tests/test_programs.py`.

**Step 1: failing tests** — update `TestCodeModifier`: the existing `test_modify_calls_api` mocks `anthropic.Anthropic`; rewrite to patch `litellm.completion` returning a litellm-shaped response (`MagicMock` with `.choices[0].message.content = '```json ...'`). Add: `test_modify_uses_program_model_override` (seed `program_models["code_modifier"] = {"model": "openai/gpt-4.1-mini"}` in an `initialized_repo` config, assert `litellm.completion` was called with that model) and `test_modify_defaults_to_config_model`. Add `assert "import anthropic" not in Path("plumb/programs/code_modifier.py").read_text()`.

**Step 3: implement** — `CodeModifier.__init__(self, repo_root=None)`; `modify(...)` resolves `model = program_models["code_modifier"].model or cfg.model or DEFAULT_MODEL` and calls `litellm.completion(model=model, max_tokens=16000, messages=[...])`; parse `response.choices[0].message.content` with the existing `_parse_response`. Check the call sites of `CodeModifier` (`plumb/cli.py` `_run_modify` and any tests) for the constructor change; keep an optional injection point for tests (accept a `completion_fn` kwarg defaulting to `litellm.completion`) so patching stays easy. Then remove `anthropic` from `[project] dependencies` in `pyproject.toml` **after** `grep -rn "import anthropic" plumb/` shows no hits; note that litellm still talks to Anthropic without the SDK. Run `uv sync` and the full suite to prove nothing needed it.

Commit: `refactor(modify): code modifier calls litellm; drop the anthropic SDK dependency`

---

## Task 4: `plumb model` command

**Files:** `plumb/cli.py`; tests in `tests/test_cli.py`.

**Step 1: failing tests**

```python
def test_model_command_prints_and_sets(initialized_repo, monkeypatch):
    from click.testing import CliRunner
    from plumb.cli import cli
    from plumb.config import load_config
    monkeypatch.chdir(initialized_repo)
    r = CliRunner().invoke(cli, ["model"])
    assert r.exit_code == 0 and "anthropic/claude-haiku-4-5" in r.output and "default" in r.output
    with patch("plumb.cli.validate_api_access") as va:
        r = CliRunner().invoke(cli, ["model", "groq/llama-3.3-70b-versatile"])
    assert r.exit_code == 0, r.output
    va.assert_called_once()
    assert load_config(initialized_repo).model == "groq/llama-3.3-70b-versatile"
    r = CliRunner().invoke(cli, ["model"])
    assert "groq/llama-3.3-70b-versatile" in r.output and "config" in r.output


def test_model_command_does_not_save_on_failed_check(initialized_repo, monkeypatch):
    from click.testing import CliRunner
    from plumb.cli import cli
    from plumb.config import load_config, DEFAULT_MODEL
    from plumb import PlumbAuthError
    monkeypatch.chdir(initialized_repo)
    with patch("plumb.cli.validate_api_access", side_effect=PlumbAuthError("GROQ_API_KEY is not set")):
        r = CliRunner().invoke(cli, ["model", "groq/llama-3.3-70b-versatile"])
    assert r.exit_code == 1 and "GROQ_API_KEY" in r.output
    assert load_config(initialized_repo).model == DEFAULT_MODEL


def test_model_command_shows_program_overrides(initialized_repo, monkeypatch):
    from click.testing import CliRunner
    from plumb.cli import cli
    from plumb.config import load_config, save_config
    monkeypatch.chdir(initialized_repo)
    cfg = load_config(initialized_repo)
    cfg.program_models = {"decision_deduplicator": {"model": "groq/x", "max_tokens": 8192}}
    save_config(initialized_repo, cfg)
    r = CliRunner().invoke(cli, ["model"])
    assert "decision_deduplicator" in r.output and "groq/x" in r.output
```

**Step 3: implement** — mirror `plumb mode`: no-arg prints `"{model}  (from {source})"` where source is `config` when the stored value differs from `DEFAULT_MODEL` else `default`, plus one line per `program_models` override; with an arg, run `validate_api_access(repo_root, model=<arg>)` (imported at module level of `cli` so the test patch of `plumb.cli.validate_api_access` intercepts), print "Testing <model>…" before and a success line after, save only on success, exit 1 with the error message on `PlumbAuthError`.

Commit: `feat(cli): plumb model shows and sets the inference model, testing connectivity before saving`

---

## Task 5: `plumb init` model step

**Files:** `plumb/cli.py` (`init`); tests in `tests/test_cli.py`; input-line updates in `tests/test_cli.py`, `tests/test_cli_extended.py`, `tests/test_integration.py`, `tests/e2e/test_record_mode.py` (the `world` fixture's init input).

**Step 1: failing tests**

```python
def test_init_confirms_default_model(tmp_repo, ...):
    # existing init-test pattern; extra input line "" (accept default via Y)
    ...
    assert load_config(tmp_repo).model == DEFAULT_MODEL


def test_init_walkthrough_picks_provider(tmp_repo, ...):
    # answer: n → provider "groq" → accept the suggested string
    ...
    assert load_config(tmp_repo).model == "groq/llama-3.3-70b-versatile"
```

**Step 3: implement** — after the mode prompt: `click.confirm(f"Plumb will use {DEFAULT_MODEL} for analysis. Use this model?", default=True)`; on decline, `click.prompt("Provider", type=click.Choice(["anthropic","openai","groq","gemini","ollama","other"]))`, then `click.prompt("Model", default=PROVIDER_SUGGESTIONS[provider])` (for `other`, no default) with `PROVIDER_SUGGESTIONS` as a module dict matching the design table; store `cfg.model`.

**Carry-forward from the Tasks 1–2 review (verified):** the fresh `init` path (`plumb/cli.py:~269-283`) builds `PlumbConfig(...)` and has NO "Verifying API access" step at all — only `_init_clone_setup` verifies. So this task must: (a) pass the chosen model into the `PlumbConfig(...)` constructor, (b) add an explicit `validate_api_access(repo_root, model=<chosen>)` call to the fresh path after the config is saved (mirroring the clone path's failure guidance), so the verified model is guaranteed to be the persisted one. Test: a fresh init with a declined default and a chosen model calls `validate_api_access` with that model (patch it), and a `PlumbAuthError` there exits 1 without leaving a half-initialized state that a re-run cannot fix (re-running init over the created `.plumb/` must work — check what the clone-detection at the top of init does with an existing config).

Then update every scripted init (grep `plumb init` and `init]` invocations across the four test files) with one extra `\n` for the confirm, and for the e2e `world` fixture change `"spec.md\ntests/\nrecord\n"` to `"spec.md\ntests/\nrecord\n\n"`. Run the full suite; only input strings may change in existing tests.

Commit: `feat(init): confirm the default model or walk through provider options`

---

## Task 6: docs

**Files:** `README.md`, `plumb_spec.md`, both `SKILL.md` copies, `docs/plans/2026-08-31-provider-neutral-inference-design.md` (Status → Implemented).

- README: Initialize section — the key line becomes provider-relative ("Plumb defaults to Anthropic's Haiku model, so it needs an `ANTHROPIC_API_KEY`. If you configure a different provider with `plumb model`, set that provider's key instead."); Requirements section likewise; command table gains `plumb model [<litellm-string>]`; "What init sets up" list mentions the model question in the Initialize sentence ("where your spec markdown lives, where your tests live, which mode Plumb should run in, and which model it should use").
- Spec: Design Principles "Inference via Claude" → inference through litellm model strings with `anthropic/claude-haiku-4-5` as the default; config schema gains `model`; init flow gains the confirm/walkthrough; new `plumb model` command section (test-before-save rule); `code_modifier` section says litellm, not "the Anthropic API directly"; dependencies list drops `anthropic`.
- SKILL.md (both, byte-identical): command row for `plumb model`.
- Run the full bare suite once more.

Commit: `docs: provider-neutral inference (config model, plumb model, provider-aware keys)`

---

## Out of scope

- Named model tiers; a picker for litellm's full catalog; provider-specific parameter tuning.
- Changing the e2e provider (stays on Haiku via `.env`).
- `validate_environment` coverage for every provider litellm supports; the smoke test is the backstop.
