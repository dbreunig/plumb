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
    spec_relevant: bool = True


class DecisionExtractorSignature(dspy.Signature):
    """Extract decisions from a conversation chunk and a diff summary.
    Decisions are explicit or implicit choices about implementation.
    Do not extract trivial decisions (variable naming, import ordering).
    Each decision should have a question framing it, the decision made,
    who made it (user or llm), and a confidence score.

    For each decision, set spec_relevant to True if the decision affects the
    system's design, behavior, architecture, data model, API surface, or
    user-facing functionality — i.e. things that belong in a specification.

    Set spec_relevant to False for:
    - Process decisions: "approve all decisions", "commit now", "run with --dry-run"
    - Git/workflow decisions: "push to main", "create a PR", "use a worktree"
    - Tooling/environment decisions: "use pytest", "install package X"
    - Superseded migration steps: intermediate states replaced by later choices
    - Meta-conversation: "let me think about that", "sounds good"

    When in doubt, default to True (safe direction)."""

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
