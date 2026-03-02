from __future__ import annotations

import dspy
from pydantic import BaseModel


class RequirementCoverage(BaseModel):
    requirement_id: str
    implemented: bool
    evidence: str


class CodeCoverageMapperSignature(dspy.Signature):
    """Determine which spec requirements are implemented in the source code.

    For each requirement, decide whether the codebase implements it and provide
    a brief evidence string (e.g. the function or module that implements it).
    Only mark a requirement as implemented when you are confident the code
    actually fulfils the stated behaviour.
    """

    requirements: str = dspy.InputField(
        desc="JSON list of {id, text} — the spec requirements"
    )
    source_summaries: str = dspy.InputField(
        desc="Summaries of source files: file path, classes, functions, docstrings"
    )
    coverage: list[RequirementCoverage] = dspy.OutputField(
        desc="List indicating whether each requirement is implemented"
    )


class CodeCoverageMapper(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(CodeCoverageMapperSignature)

    def forward(
        self, requirements: str, source_summaries: str
    ) -> list[RequirementCoverage]:
        result = self.predict(
            requirements=requirements,
            source_summaries=source_summaries,
        )
        return result.coverage
