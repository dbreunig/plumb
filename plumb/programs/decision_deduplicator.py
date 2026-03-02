from __future__ import annotations

import dspy


class DecisionDeduplicatorSignature(dspy.Signature):
    """Filter candidate decisions by removing duplicates and countermanded decisions.

    Candidates are numbered in chronological order (lower index = earlier).
    Compare candidates against each other and against existing decisions.
    Remove a candidate if it matches ANY of these rules:

    1. DUPLICATE — expresses the same choice as another candidate or an
       existing decision, even in different words. When two candidates are
       duplicates, drop the higher index (keep the first occurrence).
    2. COUNTERMANDED — a later candidate (higher index) reverses, overrides,
       or replaces an earlier candidate on the same topic. Drop the earlier
       candidate and keep the later one, since it reflects the final intent.

    Return only the indices of candidates that survive both filters."""

    candidates: str = dspy.InputField(
        desc="Numbered list of candidate decisions, e.g. '1. [Q] ... [D] ...'"
    )
    existing: str = dspy.InputField(
        desc="Numbered list of recent existing decisions for cross-reference"
    )
    unique_indices: list[int] = dspy.OutputField(
        desc="Indices from candidates to keep (1-based, only genuinely unique and not countermanded)"
    )


class DecisionDeduplicator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(DecisionDeduplicatorSignature)

    def forward(self, candidates: str, existing: str) -> list[int]:
        result = self.predict(candidates=candidates, existing=existing)
        return result.unique_indices
