from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field


class ChangeSummary(BaseModel):
    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""
    change_type: Literal[
        "feature", "bugfix", "refactor", "test", "spec", "config", "other"
    ] = "other"


class DiffAnalyzerSignature(dspy.Signature):
    """Analyze a git diff and group related changes into logical units.
    Each unit should have the files changed, a one-sentence summary,
    and a change type classification."""

    diff: str = dspy.InputField(desc="Raw unified diff string")
    change_summaries: list[ChangeSummary] = dspy.OutputField(
        desc="List of change summaries grouping related changes"
    )


class DiffAnalyzer(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(DiffAnalyzerSignature)

    def forward(self, diff: str) -> list[ChangeSummary]:
        result = self.predict(diff=diff)
        return result.change_summaries
