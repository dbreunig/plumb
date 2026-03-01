from __future__ import annotations

import dspy


class SpecUpdaterSignature(dspy.Signature):
    """Update a spec markdown section to incorporate an approved decision.
    The result of the decision should be captured as a natural requirement.
    Do not reference the decision itself. Preserve existing formatting."""

    spec_section: str = dspy.InputField(desc="Current markdown section of the spec")
    decision: str = dspy.InputField(desc="The approved decision text")
    question: str = dspy.InputField(desc="The question the decision answers")
    updated_section: str = dspy.OutputField(desc="Updated markdown for the section")


class SpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(SpecUpdaterSignature)

    def forward(self, spec_section: str, decision: str, question: str) -> str:
        result = self.predict(
            spec_section=spec_section, decision=decision, question=question
        )
        return result.updated_section


class BatchSpecUpdaterSignature(dspy.Signature):
    """Update a spec markdown section to incorporate multiple approved decisions.
    Each decision should be captured as a natural requirement.
    Do not reference the decisions themselves. Preserve existing formatting."""

    spec_section: str = dspy.InputField(desc="Current markdown section of the spec")
    decisions_text: str = dspy.InputField(
        desc="Multiple decisions formatted as numbered list with questions and answers"
    )
    updated_section: str = dspy.OutputField(desc="Updated markdown for the section")


class BatchSpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(BatchSpecUpdaterSignature)

    def forward(self, spec_section: str, decisions_text: str) -> str:
        result = self.predict(
            spec_section=spec_section, decisions_text=decisions_text
        )
        return result.updated_section
