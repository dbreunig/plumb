from __future__ import annotations

import dspy
from pydantic import BaseModel


class TestMapping(BaseModel):
    test_function: str
    file_path: str
    requirement_ids: list[str]
    confidence: float


class TestMapperSignature(dspy.Signature):
    """Map existing test functions to spec requirements.

    For each test, identify which requirement(s) it validates based on what
    the test does vs what the requirement says.  Only map when confident —
    leave unmapped if unclear.  A single test may cover multiple requirements
    and a requirement may be covered by multiple tests.
    """

    requirements: str = dspy.InputField(
        desc="JSON list of {id, text} — the spec requirements"
    )
    test_summaries: str = dspy.InputField(
        desc="Test functions with file path, name, docstring, and key assertions"
    )
    mappings: list[TestMapping] = dspy.OutputField(
        desc="List of mappings from test functions to requirement IDs"
    )


class TestMapper(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(TestMapperSignature)

    def forward(self, requirements: str, test_summaries: str) -> list[TestMapping]:
        result = self.predict(
            requirements=requirements,
            test_summaries=test_summaries,
        )
        return result.mappings
