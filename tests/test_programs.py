"""Tests for DSPy programs and code modifier.
Tests validate Signature fields, Pydantic model schemas, and mock forward() calls."""

import json
from unittest.mock import MagicMock, patch

import dspy
import pytest

from plumb.programs import (
    run_with_retries, configure_dspy, validate_api_access, get_program_lm,
    ClaudeCodeLM, _claude_code_available, get_lm,
)
from plumb.config import PlumbConfig, save_config, ensure_plumb_dir
from plumb import PlumbAuthError, PlumbInferenceError
from plumb.programs.diff_analyzer import (
    ChangeSummary,
    DiffAnalyzerSignature,
    DiffAnalyzer,
)
from plumb.programs.decision_extractor import (
    ExtractedDecision,
    DecisionExtractorSignature,
    DecisionExtractor,
)
from plumb.programs.question_synthesizer import (
    QuestionSynthesizerSignature,
    QuestionSynthesizer,
)
from plumb.programs.requirement_parser import (
    ParsedRequirement,
    RequirementParserSignature,
    RequirementParser,
)
from plumb.programs.spec_updater import WholeFileSpecUpdaterSignature, WholeFileSpecUpdater
from plumb.programs.decision_deduplicator import (
    DecisionDeduplicatorSignature,
    DecisionDeduplicator,
)
from plumb.programs.test_generator import TestGeneratorSignature, TestGenerator
from plumb.programs.code_modifier import CodeModifier


class TestValidateApiAccess:
    def test_raises_when_no_backend_available(self):
        # plumb:req-60f97012
        # plumb:req-ab686eaa
        # plumb:req-222ddbbd
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs._claude_code_available", return_value=False):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(PlumbAuthError, match="No LLM backend available"):
                validate_api_access()

    def test_raises_when_key_empty_and_no_cli(self):
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}), \
             patch("plumb.programs._claude_code_available", return_value=False):
            with pytest.raises(PlumbAuthError, match="No LLM backend available"):
                validate_api_access()

    def test_falls_back_to_cli_when_no_key(self):
        """When no API key but claude CLI is available, fallback works."""
        mock_lm = MagicMock(return_value=["hello"])
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs._claude_code_available", return_value=True), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            validate_api_access()  # should not raise — CLI fallback works
            mock_lm.assert_called_once()

    def test_passes_when_key_set_and_api_works(self):
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            validate_api_access()  # should not raise
            mock_lm.assert_called_once()

    def test_raises_when_api_returns_empty(self):
        mock_lm = MagicMock(return_value="")
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            with pytest.raises(PlumbAuthError, match="empty response"):
                validate_api_access()

    def test_raises_when_api_auth_fails(self):
        mock_lm = MagicMock(side_effect=Exception("AuthenticationError: invalid api key"))
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            with pytest.raises(PlumbAuthError, match="invalid or rejected"):
                validate_api_access()

    def test_loads_dotenv_file(self):
        # plumb:req-98d8bd75
        """Verify load_dotenv is called so .env files are picked up."""
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv") as mock_load, \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            validate_api_access()
            mock_load.assert_called_once_with(override=False)


class TestClaudeCodeLM:
    """Tests for the ClaudeCodeLM backend that routes through claude CLI."""

    def test_instantiation(self):
        lm = ClaudeCodeLM(model="haiku")
        assert lm.cli_model == "haiku"
        assert lm.model == "claude-code/haiku"

    def test_forward_parses_json_result_event(self):
        """ClaudeCodeLM extracts text from the final 'result' event."""
        fake_output = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "result", "subtype": "success", "result": "hello world"},
        ])
        mock_result = MagicMock(returncode=0, stdout=fake_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM(model="haiku")
            response = lm.forward(prompt="test prompt")
            assert response == ["hello world"]

    def test_forward_handles_plain_text_fallback(self):
        """Falls back to raw stdout if JSON parsing fails."""
        mock_result = MagicMock(returncode=0, stdout="just plain text", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM(model="haiku")
            response = lm.forward(prompt="test")
            assert response == ["just plain text"]

    def test_forward_raises_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120)):
            lm = ClaudeCodeLM()
            with pytest.raises(PlumbInferenceError, match="timed out"):
                lm.forward(prompt="test")

    def test_forward_raises_on_missing_cli(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            lm = ClaudeCodeLM()
            with pytest.raises(PlumbAuthError, match="not found"):
                lm.forward(prompt="test")

    def test_forward_raises_on_nonzero_exit(self):
        mock_result = MagicMock(returncode=1, stdout="", stderr="something failed")
        with patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM()
            with pytest.raises(PlumbInferenceError, match="exited 1"):
                lm.forward(prompt="test")

    def test_forward_builds_prompt_from_messages(self):
        """Messages list is concatenated into prompt text."""
        fake_output = json.dumps([{"type": "result", "result": "ok"}])
        mock_result = MagicMock(returncode=0, stdout=fake_output, stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            lm = ClaudeCodeLM()
            lm.forward(messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Say hi."},
            ])
            call_kwargs = mock_run.call_args
            assert "You are helpful." in call_kwargs.kwargs["input"]
            assert "Say hi." in call_kwargs.kwargs["input"]

    def test_callable_delegates_to_forward(self):
        fake_output = json.dumps([{"type": "result", "result": "called"}])
        mock_result = MagicMock(returncode=0, stdout=fake_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM()
            result = lm("hello")
            assert result == ["called"]


class TestClaudeCodeAvailable:
    def test_returns_true_when_claude_on_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert _claude_code_available() is True

    def test_returns_false_when_not_installed(self):
        with patch("shutil.which", return_value=None):
            assert _claude_code_available() is False


class TestGetLm:
    def test_returns_api_lm_when_key_set(self):
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            lm = get_lm()
            assert "anthropic" in lm.model

    def test_returns_cli_lm_when_no_key_but_cli_available(self):
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs._claude_code_available", return_value=True):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            lm = get_lm()
            assert isinstance(lm, ClaudeCodeLM)

    def test_raises_when_nothing_available(self):
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("plumb.programs._claude_code_available", return_value=False):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(PlumbAuthError, match="No LLM backend available"):
                get_lm()

    def test_api_key_takes_precedence_over_cli(self):
        """When both are available, API key wins (faster)."""
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("plumb.programs._claude_code_available", return_value=True):
            lm = get_lm()
            assert not isinstance(lm, ClaudeCodeLM)


class TestRunWithRetries:
    def test_success_first_try(self):
        result = run_with_retries(lambda: 42)
        assert result == 42

    def test_retries_on_failure(self):
        # plumb:req-ab92bd9c
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        result = run_with_retries(flaky, max_retries=2)
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        # plumb:req-a8b816ec
        with pytest.raises(PlumbInferenceError):
            run_with_retries(lambda: 1 / 0, max_retries=1)

    def test_auth_error_raises_immediately(self):
        def bad_key():
            raise Exception("AuthenticationError: invalid API key")

        with pytest.raises(PlumbAuthError, match="invalid or rejected"):
            run_with_retries(bad_key, max_retries=2)

    def test_api_key_error_raises_immediately(self):
        def bad_key():
            raise Exception("API Key not found")

        with pytest.raises(PlumbAuthError, match="invalid or rejected"):
            run_with_retries(bad_key, max_retries=2)


class TestChangeSummary:
    def test_defaults(self):
        cs = ChangeSummary()
        assert cs.files_changed == []
        assert cs.change_type == "other"

    def test_valid_types(self):
        for ct in ["feature", "bugfix", "refactor", "test", "spec", "config", "other"]:
            cs = ChangeSummary(change_type=ct)
            assert cs.change_type == ct


class TestExtractedDecision:
    def test_defaults(self):
        ed = ExtractedDecision(decision="use sync")
        assert ed.made_by == "llm"
        assert ed.confidence == 0.5
        assert ed.spec_relevant is True

    def test_full(self):
        ed = ExtractedDecision(
            question="Sync or async?",
            decision="Use sync",
            made_by="user",
            confidence=0.95,
        )
        assert ed.question == "Sync or async?"

    def test_spec_relevant_false(self):
        ed = ExtractedDecision(decision="commit now", spec_relevant=False)
        assert ed.spec_relevant is False


class TestParsedRequirement:
    def test_defaults(self):
        pr = ParsedRequirement()
        assert pr.ambiguous is False

    def test_ambiguous(self):
        pr = ParsedRequirement(text="Something vague", ambiguous=True)
        assert pr.ambiguous is True


class TestDiffAnalyzerSignature:
    def test_has_correct_fields(self):
        sig = DiffAnalyzerSignature
        assert "diff" in sig.input_fields
        assert "change_summaries" in sig.output_fields


class TestDecisionExtractorSignature:
    def test_has_correct_fields(self):
        sig = DecisionExtractorSignature
        assert "chunk" in sig.input_fields
        assert "diff_summary" in sig.input_fields
        assert "decisions" in sig.output_fields


class TestQuestionSynthesizerSignature:
    def test_has_correct_fields(self):
        sig = QuestionSynthesizerSignature
        assert "decision" in sig.input_fields
        assert "question" in sig.output_fields


class TestRequirementParserSignature:
    def test_has_correct_fields(self):
        sig = RequirementParserSignature
        assert "markdown" in sig.input_fields
        assert "requirements" in sig.output_fields


class TestWholeFileSpecUpdaterSignature:
    def test_has_correct_fields(self):
        sig = WholeFileSpecUpdaterSignature
        assert "spec_content" in sig.input_fields
        assert "decisions_text" in sig.input_fields
        assert "section_updates_json" in sig.output_fields
        assert "new_sections_json" in sig.output_fields


class TestTestGeneratorSignature:
    def test_has_correct_fields(self):
        sig = TestGeneratorSignature
        assert "requirements" in sig.input_fields
        assert "existing_tests" in sig.input_fields
        assert "code_context" in sig.input_fields
        assert "test_code" in sig.output_fields


class TestDiffAnalyzerModule:
    def test_has_predict(self):
        analyzer = DiffAnalyzer()
        assert hasattr(analyzer, "predict")


class TestDecisionExtractorModule:
    def test_has_predict(self):
        extractor = DecisionExtractor()
        assert hasattr(extractor, "predict")


class TestQuestionSynthesizerModule:
    def test_has_predict(self):
        synth = QuestionSynthesizer()
        assert hasattr(synth, "predict")


class TestRequirementParserModule:
    def test_has_predict(self):
        parser = RequirementParser()
        assert hasattr(parser, "predict")


class TestWholeFileSpecUpdaterModule:
    def test_has_predict(self):
        updater = WholeFileSpecUpdater()
        assert hasattr(updater, "predict")


class TestTestGeneratorModule:
    def test_has_predict(self):
        gen = TestGenerator()
        assert hasattr(gen, "predict")


class TestDecisionDeduplicatorSignature:
    def test_has_correct_fields(self):
        sig = DecisionDeduplicatorSignature
        assert "candidates" in sig.input_fields
        assert "existing" in sig.input_fields
        assert "unique_indices" in sig.output_fields


class TestDecisionDeduplicatorModule:
    def test_has_predict(self):
        deduplicator = DecisionDeduplicator()
        assert hasattr(deduplicator, "predict")


class TestCodeModifier:
    def test_parse_response_json_block(self):
        text = '```json\n{"src/a.py": "content"}\n```'
        result = CodeModifier._parse_response(text)
        assert result == {"src/a.py": "content"}

    def test_parse_response_raw_json(self):
        text = '{"src/a.py": "content"}'
        result = CodeModifier._parse_response(text)
        assert result == {"src/a.py": "content"}

    def test_parse_response_invalid(self):
        result = CodeModifier._parse_response("no json here")
        assert result == {}

    def test_modify_calls_api(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='```json\n{"src/a.py": "modified"}\n```')
        ]
        mock_client.messages.create.return_value = mock_response

        modifier = CodeModifier(client=mock_client)
        result = modifier.modify(
            staged_diff="diff content",
            decision="Use async",
            rejection_reason="Too complex",
            spec_content="# Spec",
        )
        assert result == {"src/a.py": "modified"}
        mock_client.messages.create.assert_called_once()

    def test_prompt_includes_all_inputs(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="{}")]
        mock_client.messages.create.return_value = mock_response

        modifier = CodeModifier(client=mock_client)
        modifier.modify(
            staged_diff="my diff",
            decision="decision text",
            rejection_reason="reason text",
            spec_content="spec text",
        )
        call_args = mock_client.messages.create.call_args
        prompt = call_args[1]["messages"][0]["content"]
        assert "my diff" in prompt
        assert "decision text" in prompt
        assert "reason text" in prompt
        assert "spec text" in prompt


class TestGetProgramLm:
    def test_returns_none_when_no_config(self, tmp_path):
        """No .plumb/config.json → returns None."""
        result = get_program_lm("decision_deduplicator", repo_root=tmp_path)
        assert result is None

    def test_returns_none_when_program_not_listed(self, tmp_repo):
        """Config exists but program_models is empty → returns None."""
        ensure_plumb_dir(tmp_repo)
        cfg = PlumbConfig(spec_paths=["spec.md"])
        save_config(tmp_repo, cfg)
        result = get_program_lm("decision_deduplicator", repo_root=tmp_repo)
        assert result is None

    def test_returns_lm_when_override_exists(self, tmp_repo):
        """Config has an override → returns a dspy.LM."""
        ensure_plumb_dir(tmp_repo)
        cfg = PlumbConfig(
            spec_paths=["spec.md"],
            program_models={
                "decision_deduplicator": {"model": "openai/gpt-4o-mini", "max_tokens": 4096},
            },
        )
        save_config(tmp_repo, cfg)
        lm = get_program_lm("decision_deduplicator", repo_root=tmp_repo)
        assert isinstance(lm, dspy.LM)
        assert lm.model == "openai/gpt-4o-mini"
        assert lm.kwargs["max_tokens"] == 4096

    def test_returns_none_when_no_repo_root(self):
        """No repo root found → returns None."""
        with patch("plumb.config.find_repo_root", return_value=None):
            result = get_program_lm("decision_deduplicator")
            assert result is None
