from __future__ import annotations

import dspy


class DecisionDeduplicatorSignature(dspy.Signature):
    """Identify semantic duplicates among candidate decisions.

    Compare candidates against each other and against existing decisions.
    Two decisions are duplicates if they express the same choice, even in
    different words. Return only the indices of genuinely unique candidates.
    When two candidates are duplicates, prefer the one with the lower index
    (first occurrence)."""

    candidates: str = dspy.InputField(
        desc="Numbered list of candidate decisions, e.g. '1. [Q] ... [D] ...'"
    )
    existing: str = dspy.InputField(
        desc="Numbered list of recent existing decisions for cross-reference"
    )
    unique_indices: list[int] = dspy.OutputField(
        desc="Indices from candidates to keep (1-based, only genuinely unique)"
    )


class DecisionDeduplicator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(DecisionDeduplicatorSignature)

    def forward(self, candidates: str, existing: str) -> list[int]:
        result = self.predict(candidates=candidates, existing=existing)
        return result.unique_indices
