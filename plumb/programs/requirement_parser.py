from __future__ import annotations

import dspy
from pydantic import BaseModel, Field


class ParsedRequirement(BaseModel):
    text: str = ""
    ambiguous: bool = False


class RequirementParserSignature(dspy.Signature):
    """Parse markdown spec text into explicit, testable requirement statements.
    Rules: atomic statements, active voice, no duplicates.
    Vague or ambiguous statements should be flagged with ambiguous=true."""

    markdown: str = dspy.InputField(desc="Markdown spec text")
    requirements: list[ParsedRequirement] = dspy.OutputField(
        desc="List of parsed requirements"
    )


class RequirementParser(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(RequirementParserSignature)

    def forward(self, markdown: str) -> list[ParsedRequirement]:
        result = self.predict(markdown=markdown)
        return result.requirements
