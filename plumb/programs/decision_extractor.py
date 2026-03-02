from __future__ import annotations

from typing import Optional

import dspy
from pydantic import BaseModel, Field


class ExtractedDecision(BaseModel):
    question: Optional[str] = None
    decision: str = ""
    made_by: str = "llm"
    confidence: float = 0.5
    related_diff_summary: Optional[str] = None


class DecisionExtractorSignature(dspy.Signature):
    """Extract decisions from a conversation chunk and a diff summary.
    Decisions are explicit or implicit choices about implementation.
    Do not extract trivial decisions (variable naming, import ordering).
    Each decision should have a question framing it, the decision made,
    who made it (user or llm), and a confidence score."""

    chunk: str = dspy.InputField(desc="Single conversation chunk text")
    diff_summary: str = dspy.InputField(desc="Output of DiffAnalyzer")
    decisions: list[ExtractedDecision] = dspy.OutputField(
        desc="List of extracted decisions"
    )


class DecisionExtractor(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(DecisionExtractorSignature)

    def forward(self, chunk: str, diff_summary: str) -> list[ExtractedDecision]:
        result = self.predict(chunk=chunk, diff_summary=diff_summary)
        return result.decisions
