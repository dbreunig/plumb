# Provider-neutral inference

**Date:** 2026-08-31
**Status:** Proposed

## Problem

Plumb's inference already runs through DSPy and litellm, and the per-program
`program_models` overrides accept any litellm model string (this repo runs its
deduplicator on Groq). But the default model is hardcoded to Anthropic in four
places, the API-key check tests the literal `ANTHROPIC_API_KEY` env var, and
`code_modifier.py` calls the raw `anthropic` SDK. A user who wants OpenAI,
Groq, Gemini, or a local Ollama model cannot get there through configuration.

## Decision

Make the default model a config field holding a litellm model string, resolve
every call site through it, make the key check provider-aware, and give users
two surfaces: a Y/n step in `plumb init` (accept the default or walk through
provider options) and a `plumb model` command to inspect or change it later,
per project. Anthropic Haiku stays the shipped default, so zero-config
behavior is unchanged.

## Design

### Config

```python
DEFAULT_MODEL = "anthropic/claude-haiku-4-5"

class PlumbConfig(BaseModel):
    model: str = DEFAULT_MODEL   # litellm model string
```

- `get_lm(repo_root=None)` loads the config (via `find_repo_root` when no
  root is given) and builds `dspy.LM(cfg.model, max_tokens=28000)`; missing
  or unloadable config falls back to `DEFAULT_MODEL`.
- `program_models` entries stay the per-program override, documented as the
  same litellm string format.
- `_llm_dedup`'s hardcoded fallback becomes `get_lm()`.

### Provider-aware key check

`validate_api_access(repo_root=None, model=None)`:

1. Load `.env` as today.
2. Ask litellm which env vars the model's provider needs
   (`litellm.validate_environment(model)`) and raise `PlumbAuthError` naming
   the exact missing variable, e.g. "GROQ_API_KEY is not set". Providers that
   need no key (Ollama) skip this step.
3. Run the existing one-line smoke test with the configured LM.

`plumb init`'s failure guidance prints the provider's env var instead of
always saying `ANTHROPIC_API_KEY`.

### `code_modifier` drops the Anthropic SDK

The one non-DSPy call site moves to `litellm.completion(model=..., messages=...)`,
resolving its model from `program_models["code_modifier"]` and falling back to
the config model. The `anthropic` package leaves the hard dependencies
(litellm speaks to Anthropic over HTTP itself).

### `plumb model`

- `plumb model` prints the effective model and its source (`config` or
  `default`), plus any `program_models` overrides.
- `plumb model <litellm-string>` smoke tests the model first, and only saves
  it to `.plumb/config.json` on success. On failure it exits 1, names the
  missing env var when that is the cause, and leaves the config unchanged.

### `plumb init`

After the mode question:

```
Plumb will use anthropic/claude-haiku-4-5 for analysis. Use this model? [Y/n]
```

Enter accepts the default. On `n`, init walks through the options: pick a
provider from a short list (anthropic, openai, groq, gemini, ollama, other),
see a suggested model string for that provider, and edit or accept it. The
existing "Verifying API access" step then tests whatever was chosen, so a bad
choice fails at init with a clear message, not at the first commit.

Suggested strings per provider (editable, not exhaustive):

| Provider | Suggestion |
|---|---|
| anthropic | `anthropic/claude-haiku-4-5` |
| openai | `openai/gpt-4.1-mini` |
| groq | `groq/llama-3.3-70b-versatile` |
| gemini | `gemini/gemini-2.0-flash` |
| ollama | `ollama/llama3.1` |
| other | free-form litellm string |

### Docs

README's Initialize section says the key belongs to whichever provider you
configure, Anthropic by default. The spec's "Inference via Claude" principle
becomes "Inference through litellm model strings, Anthropic Haiku by
default", and the config schema, init flow, and command table gain the new
field and command. Both `SKILL.md` copies gain the `plumb model` row.

## Non-goals

- Named model tiers or per-stage defaults beyond the existing
  `program_models`.
- Validating that a model string is a known litellm model before the smoke
  test (the smoke test is the validation).
- Changing the e2e provider (it stays on Haiku).

## Compatibility notes

- Old configs without `model` load with the default (pydantic default).
- `plumb init` gains a fourth prompt, so every scripted init (tests, e2e)
  needs one more input line; an empty line accepts the default.
- Changing the model affects future extraction only. Stored decisions,
  digests, and provenance are model-independent.
