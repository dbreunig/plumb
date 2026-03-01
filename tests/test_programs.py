"""Tests for DSPy programs and code modifier.
Tests validate Signature fields, Pydantic model schemas, and mock forward() calls."""

import json
from unittest.mock import MagicMock, patch

import dspy

from plumb.programs import run_with_retries, configure_dspy
from plumb import PlumbInferenceError
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
from plumb.programs.spec_updater import SpecUpdaterSignature, SpecUpdater
from plumb.programs.test_generator import TestGeneratorSignature, TestGenerator
from plumb.programs.code_modifier import CodeModifier


class TestRunWithRetries:
    def test_success_first_try(self):
        result = run_with_retries(lambda: 42)
        assert result == 42

    def test_retries_on_failure(self):
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
        import pytest

        with pytest.raises(PlumbInferenceError):
            run_with_retries(lambda: 1 / 0, max_retries=1)


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

    def test_full(self):
        ed = ExtractedDecision(
            question="Sync or async?",
            decision="Use sync",
            made_by="user",
            confidence=0.95,
        )
        assert ed.question == "Sync or async?"


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


class TestSpecUpdaterSignature:
    def test_has_correct_fields(self):
        sig = SpecUpdaterSignature
        assert "spec_section" in sig.input_fields
        assert "decision" in sig.input_fields
        assert "question" in sig.input_fields
        assert "updated_section" in sig.output_fields


class TestTestGeneratorSignature:
    def test_has_correct_fields(self):
        sig = TestGeneratorSignature
        assert "requirements" in sig.input_fields
        assert "existing_tests" in sig.input_fields
        assert "code_context" in sig.input_fields
        assert "test_stubs" in sig.output_fields


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


class TestSpecUpdaterModule:
    def test_has_predict(self):
        updater = SpecUpdater()
        assert hasattr(updater, "predict")


class TestTestGeneratorModule:
    def test_has_predict(self):
        gen = TestGenerator()
        assert hasattr(gen, "predict")


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
