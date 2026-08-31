"""Tests for DSPy programs and code modifier.
Tests validate Signature fields, Pydantic model schemas, and mock forward() calls."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import dspy
import pytest

from plumb.programs import run_with_retries, configure_dspy, validate_api_access, get_program_lm
from plumb.config import (
    DEFAULT_MODEL,
    PlumbConfig,
    ensure_plumb_dir,
    load_config,
    save_config,
)
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


_ENV_OK = {"keys_in_environment": True, "missing_keys": []}


class TestValidateApiAccess:
    def test_raises_when_key_missing(self, tmp_path, monkeypatch):
        # plumb:req-60f97012
        # plumb:req-ab686eaa
        # plumb:req-222ddbbd
        monkeypatch.chdir(tmp_path)  # not a repo: the default model applies
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment",
                   return_value={"keys_in_environment": False,
                                 "missing_keys": ["ANTHROPIC_API_KEY"]}) as ve:
            with pytest.raises(PlumbAuthError, match="ANTHROPIC_API_KEY is not set"):
                validate_api_access()
        ve.assert_called_once_with("anthropic/claude-haiku-4-5")

    def test_raises_when_key_empty(self, tmp_path, monkeypatch):
        """An empty key counts as missing (litellm reports it in missing_keys)."""
        monkeypatch.chdir(tmp_path)
        with patch("dotenv.load_dotenv"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}), \
             patch("litellm.validate_environment",
                   return_value={"keys_in_environment": False,
                                 "missing_keys": ["ANTHROPIC_API_KEY"]}):
            with pytest.raises(PlumbAuthError, match="ANTHROPIC_API_KEY is not set"):
                validate_api_access()

    def test_passes_when_key_set_and_api_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            validate_api_access()  # should not raise
            mock_lm.assert_called_once()

    def test_raises_when_api_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_lm = MagicMock(return_value="")
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            with pytest.raises(PlumbAuthError, match="empty response"):
                validate_api_access()

    def test_raises_when_api_auth_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_lm = MagicMock(side_effect=Exception("AuthenticationError: invalid api key"))
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            with pytest.raises(PlumbAuthError, match="invalid or rejected"):
                validate_api_access()

    def test_loads_dotenv_file(self, tmp_path, monkeypatch):
        # plumb:req-98d8bd75
        """Verify load_dotenv is called so .env files are picked up."""
        monkeypatch.chdir(tmp_path)
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv") as mock_load, \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.get_lm", return_value=mock_lm):
            validate_api_access()
            mock_load.assert_called_once_with(override=False)

    def test_names_missing_provider_key(self, tmp_repo, monkeypatch):
        """The error names the exact missing env var and the configured model."""
        save_config(tmp_repo, PlumbConfig(model="groq/llama-3.3-70b-versatile"))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment",
                   return_value={"keys_in_environment": False,
                                 "missing_keys": ["GROQ_API_KEY"]}) as ve:
            with pytest.raises(PlumbAuthError, match="GROQ_API_KEY") as excinfo:
                validate_api_access(repo_root=tmp_repo)
        ve.assert_called_once_with("groq/llama-3.3-70b-versatile")
        assert "groq/llama-3.3-70b-versatile" in str(excinfo.value)

    def test_names_multiple_missing_keys_with_plural(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment",
                   return_value={"keys_in_environment": False,
                                 "missing_keys": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]}):
            with pytest.raises(PlumbAuthError) as excinfo:
                validate_api_access()
        msg = str(excinfo.value)
        assert "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY are not set" in msg
        assert "Set them in a .env file" in msg

    def test_smoke_tests_when_keys_present(self, tmp_repo):
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.get_lm", return_value=mock_lm) as gl:
            validate_api_access(repo_root=tmp_repo)
        mock_lm.assert_called_once()
        gl.assert_called_once_with(tmp_repo)

    def test_ollama_missing_api_base_raises(self, tmp_repo):
        """litellm requires OLLAMA_API_BASE for ollama models; its absence is named."""
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment",
                   return_value={"keys_in_environment": False,
                                 "missing_keys": ["OLLAMA_API_BASE"]}) as ve:
            with pytest.raises(PlumbAuthError, match="OLLAMA_API_BASE"):
                validate_api_access(repo_root=tmp_repo, model="ollama/llama3.1")
        ve.assert_called_once_with("ollama/llama3.1")

    def test_model_arg_builds_lm_for_smoke_test(self, tmp_repo):
        """An explicit model arg is smoke tested directly, not via the config."""
        mock_lm = MagicMock(return_value="hello")
        with patch("dotenv.load_dotenv"), \
             patch("litellm.validate_environment", return_value=_ENV_OK), \
             patch("plumb.programs.dspy.LM", return_value=mock_lm) as lm_cls:
            validate_api_access(repo_root=tmp_repo, model="ollama/llama3.1")
        lm_cls.assert_called_once_with("ollama/llama3.1", max_tokens=28000)
        mock_lm.assert_called_once()


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
        assert ed.made_by == "agent"
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
        assert "duplicate_indices" in sig.output_fields


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

    @staticmethod
    def _completion_response(text):
        """A litellm-shaped completion response."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = text
        return response

    def test_modify_calls_api(self, tmp_path):
        with patch("litellm.completion") as mock_completion:
            mock_completion.return_value = self._completion_response(
                '```json\n{"src/a.py": "modified"}\n```'
            )
            modifier = CodeModifier(repo_root=tmp_path)
            result = modifier.modify(
                staged_diff="diff content",
                decision="Use async",
                rejection_reason="Too complex",
                spec_content="# Spec",
            )
        assert result == {"src/a.py": "modified"}
        mock_completion.assert_called_once()

    def test_prompt_includes_all_inputs(self, tmp_path):
        fake = MagicMock(return_value=self._completion_response("{}"))
        modifier = CodeModifier(repo_root=tmp_path, completion_fn=fake)
        modifier.modify(
            staged_diff="my diff",
            decision="decision text",
            rejection_reason="reason text",
            spec_content="spec text",
        )
        prompt = fake.call_args.kwargs["messages"][0]["content"]
        assert "my diff" in prompt
        assert "decision text" in prompt
        assert "reason text" in prompt
        assert "spec text" in prompt

    def test_modify_defaults_to_default_model(self, tmp_path):
        """No .plumb config at all: falls back to DEFAULT_MODEL, max_tokens 16000."""
        fake = MagicMock(return_value=self._completion_response("{}"))
        CodeModifier(repo_root=tmp_path, completion_fn=fake).modify(
            staged_diff="d", decision="x", rejection_reason="r", spec_content="s",
        )
        kwargs = fake.call_args.kwargs
        assert kwargs["model"] == DEFAULT_MODEL
        assert kwargs["max_tokens"] == 16000

    def test_modify_defaults_to_config_model(self, initialized_repo):
        """No per-program override: uses the config-wide model."""
        cfg = load_config(initialized_repo)
        cfg.model = "groq/llama-3.3-70b-versatile"
        save_config(initialized_repo, cfg)
        fake = MagicMock(return_value=self._completion_response("{}"))
        CodeModifier(repo_root=initialized_repo, completion_fn=fake).modify(
            staged_diff="d", decision="x", rejection_reason="r", spec_content="s",
        )
        kwargs = fake.call_args.kwargs
        assert kwargs["model"] == "groq/llama-3.3-70b-versatile"
        assert kwargs["max_tokens"] == 16000

    def test_modify_uses_program_model_override(self, initialized_repo):
        """program_models["code_modifier"] wins; max_tokens defaults to 16000."""
        cfg = load_config(initialized_repo)
        cfg.program_models = {"code_modifier": {"model": "openai/gpt-4.1-mini"}}
        save_config(initialized_repo, cfg)
        fake = MagicMock(return_value=self._completion_response("{}"))
        CodeModifier(repo_root=initialized_repo, completion_fn=fake).modify(
            staged_diff="d", decision="x", rejection_reason="r", spec_content="s",
        )
        kwargs = fake.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-4.1-mini"
        assert kwargs["max_tokens"] == 16000

    def test_modify_honors_override_max_tokens(self, initialized_repo):
        cfg = load_config(initialized_repo)
        cfg.program_models = {
            "code_modifier": {"model": "openai/gpt-4.1-mini", "max_tokens": 4096},
        }
        save_config(initialized_repo, cfg)
        fake = MagicMock(return_value=self._completion_response("{}"))
        CodeModifier(repo_root=initialized_repo, completion_fn=fake).modify(
            staged_diff="d", decision="x", rejection_reason="r", spec_content="s",
        )
        assert fake.call_args.kwargs["max_tokens"] == 4096

    def test_no_anthropic_import(self):
        import plumb.programs.code_modifier as cm
        source = Path(cm.__file__).read_text()
        assert "import anthropic" not in source


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


class TestGetLm:
    def test_get_lm_uses_configured_model(self, tmp_repo):
        from plumb.programs import get_lm
        save_config(tmp_repo, PlumbConfig(model="openai/gpt-4.1-mini"))
        lm = get_lm(repo_root=tmp_repo)
        assert lm.model == "openai/gpt-4.1-mini"

    def test_get_lm_falls_back_to_default_without_config(self, tmp_path, monkeypatch):
        from plumb.config import DEFAULT_MODEL
        from plumb.programs import get_lm
        monkeypatch.chdir(tmp_path)          # not a repo, no config
        assert get_lm().model == DEFAULT_MODEL

    def test_get_lm_falls_back_to_default_when_repo_has_no_config(self, tmp_repo):
        from plumb.config import DEFAULT_MODEL
        from plumb.programs import get_lm
        assert get_lm(repo_root=tmp_repo).model == DEFAULT_MODEL
