from __future__ import annotations

import dspy


class DecisionDeduplicatorSignature(dspy.Signature):
    """Identify candidate decisions that should be REMOVED as duplicates or
    countermanded, returning their indices. An empty list means keep everything.

    Candidates are numbered in chronological order (lower index = earlier).
    Compare candidates against each other and against existing decisions.
    Mark a candidate for removal ONLY if it matches one of these rules:

    1. DUPLICATE — expresses the SAME choice about the SAME thing as another
       candidate or an existing decision, even in different words. When two
       candidates are duplicates, remove the higher index (keep the first
       occurrence).
    2. COUNTERMANDED — a later candidate (higher index) reverses, overrides,
       or replaces an earlier candidate on the same topic. Remove the earlier
       candidate; the later one reflects the final intent.

    A candidate is NOT a duplicate merely because it concerns the same file,
    module, or feature area as an existing decision. A new method, parameter,
    behavior, or constraint is a distinct decision even when it builds on a
    prior one (e.g. "add a burst allowance" and "add remaining() to expose
    unused quota" are two decisions, not one).

    When uncertain, do NOT remove. A wrongly removed decision is silent data
    loss; a kept near-duplicate is visible and cheap to ignore later. If no
    candidate matches a rule, return an empty list."""

    candidates: str = dspy.InputField(
        desc="Numbered list of candidate decisions, e.g. '1. [Q] ... [D] ...'"
    )
    existing: str = dspy.InputField(
        desc="Numbered list of recent existing decisions for cross-reference"
    )
    duplicate_indices: list[int] = dspy.OutputField(
        desc="1-based indices of candidates to REMOVE (duplicate or countermanded); empty list if all should be kept"
    )


class DecisionDeduplicator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(DecisionDeduplicatorSignature)

    def forward(self, candidates: str, existing: str) -> list[int]:
        result = self.predict(candidates=candidates, existing=existing)
        return result.duplicate_indices
