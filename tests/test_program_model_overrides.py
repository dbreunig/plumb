"""Tests that program_models config overrides actually reach the LLM call site.

The contract: when a user puts an entry in program_models for a given program,
that LM — not the global default — must be the one that receives the prompt.

These tests don't verify get_program_lm() in isolation (that's in test_programs.py).
They verify the end-to-end wiring: config → get_program_lm → dspy.context → program call.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import dspy
import pytest

from plumb.config import PlumbConfig, save_config, ensure_plumb_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo_with_override(tmp_repo: Path, program_name: str, model: str) -> Path:
    """Set up a plumb repo with a single program_models override."""
    ensure_plumb_dir(tmp_repo)
    cfg = PlumbConfig(
        spec_paths=["spec.md"],
        test_paths=["tests/"],
        program_models={program_name: {"model": model}},
    )
    save_config(tmp_repo, cfg)
    return tmp_repo


def _make_requirements_file(repo: Path, reqs: list[dict]) -> None:
    """Write a requirements.json that check_spec_to_code_coverage expects."""
    req_path = repo / ".plumb" / "requirements.json"
    req_path.write_text(json.dumps(reqs))


def _make_source_file(repo: Path, name: str, content: str) -> None:
    """Create a Python source file in the repo."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Core principle: the override LM must be the one that receives the call
# ---------------------------------------------------------------------------


class TestCoverageMapperUsesOverride:
    """When program_models has 'code_coverage_mapper', coverage mapping
    must use that LM, not the global default."""

    def test_override_lm_receives_the_call(self, tmp_repo):
        """The configured override LM should be invoked, not the default."""
        repo = _make_repo_with_override(tmp_repo, "code_coverage_mapper", "anthropic/claude-haiku-4-5-20251001")
        _make_requirements_file(repo, [
            {"id": "req-1", "text": "The system must do X."},
        ])
        _make_source_file(repo, "app.py", "def do_x():\n    pass\n")

        # Track which LM actually gets called
        called_models = []

        original_forward = dspy.Predict.forward

        def tracking_forward(self, **kwargs):
            # Inside dspy.context, dspy.settings.lm reflects the active LM
            active_lm = dspy.settings.lm
            called_models.append(active_lm.model)
            # Return a plausible result so the pipeline doesn't crash
            from plumb.programs.code_coverage_mapper import RequirementCoverage
            mock_result = MagicMock()
            mock_result.coverage = [
                RequirementCoverage(requirement_id="req-1", implemented=False, evidence=""),
            ]
            return mock_result

        with patch.object(dspy.Predict, "forward", tracking_forward), \
             patch("plumb.programs.configure_dspy"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)

            from plumb.coverage_reporter import check_spec_to_code_coverage
            check_spec_to_code_coverage(repo, use_llm=True)

        assert len(called_models) >= 1, "DSPy Predict was never called"
        # The override model should have been used (ClaudeCodeLM strips 'anthropic/' prefix)
        assert any("haiku" in m for m in called_models), (
            f"Expected haiku override to be active, but saw: {called_models}"
        )

class TestTestMapperUsesOverride:
    """When program_models has 'test_mapper', the test mapping command
    must use that LM."""

    def test_override_lm_receives_the_call(self, tmp_repo):
        repo = _make_repo_with_override(tmp_repo, "test_mapper", "anthropic/claude-haiku-4-5-20251001")

        called_models = []

        def tracking_forward(self, **kwargs):
            active_lm = dspy.settings.lm
            called_models.append(active_lm.model)
            mock_result = MagicMock()
            mock_result.mappings = []
            return mock_result

        with patch.object(dspy.Predict, "forward", tracking_forward), \
             patch("plumb.programs.configure_dspy"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)

            from plumb.programs import run_chunked_mapper, get_program_lm
            from plumb.programs.test_mapper import TestMapper

            mapper = TestMapper()
            override_lm = get_program_lm("test_mapper", repo)

            assert override_lm is not None, "Override should have been returned"

            req_json = json.dumps([{"id": "req-1", "text": "Must do X"}])
            items = [("test_foo", json.dumps({"name": "test_foo", "file": "tests/test_foo.py"}))]

            def _combine(chunk):
                return json.dumps([json.loads(t) for _, t in chunk])

            with dspy.context(lm=override_lm):
                run_chunked_mapper(mapper, req_json, items, budget=60000, combine_fn=_combine)

        assert len(called_models) >= 1
        assert any("haiku" in m for m in called_models), (
            f"Expected haiku override, but saw: {called_models}"
        )


class TestRequirementParserUsesOverride:
    """When program_models has 'requirement_parser', spec parsing
    must use that LM."""

    def test_override_lm_receives_the_call(self, tmp_repo):
        repo = _make_repo_with_override(tmp_repo, "requirement_parser", "anthropic/claude-haiku-4-5-20251001")

        # Create a spec file the parser will read
        spec = repo / "spec.md"
        spec.write_text("# Spec\n\n## Features\n\nThe system must do X.\n")

        called_models = []

        def tracking_forward(self, **kwargs):
            active_lm = dspy.settings.lm
            called_models.append(active_lm.model)
            from plumb.programs.requirement_parser import ParsedRequirement
            mock_result = MagicMock()
            mock_result.requirements = [
                ParsedRequirement(text="The system must do X.", ambiguous=False),
            ]
            return mock_result

        with patch.object(dspy.Predict, "forward", tracking_forward), \
             patch("plumb.programs.configure_dspy"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)

            from plumb.sync import parse_spec_files
            parse_spec_files(repo)

        assert len(called_models) >= 1
        assert any("haiku" in m for m in called_models), (
            f"Expected haiku override, but saw: {called_models}"
        )


# ---------------------------------------------------------------------------
# Negative case: override for one program must not leak to another
# ---------------------------------------------------------------------------


class TestOverrideIsolation:
    """An override for program A must not affect program B."""

    def test_coverage_mapper_override_does_not_affect_other_programs(self, tmp_repo):
        """Configuring code_coverage_mapper should not change the LM for
        requirement_parser."""
        repo = _make_repo_with_override(tmp_repo, "code_coverage_mapper", "anthropic/claude-haiku-4-5-20251001")

        from plumb.programs import get_program_lm

        with patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)

            coverage_lm = get_program_lm("code_coverage_mapper", repo)
            parser_lm = get_program_lm("requirement_parser", repo)

        assert coverage_lm is not None, "Coverage mapper override should exist"
        assert parser_lm is None, "Requirement parser should have no override"
