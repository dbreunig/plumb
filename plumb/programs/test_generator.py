from __future__ import annotations

import dspy


class TestGeneratorSignature(dspy.Signature):
    """Generate pytest test stubs for uncovered requirements.
    Rules: one function per requirement, descriptive names
    (test_<req_id>_<description>), stubs include '# TODO: implement'
    and pytest.skip(), do not overwrite existing tests."""

    requirements: str = dspy.InputField(desc="Uncovered requirements as text")
    existing_tests: str = dspy.InputField(desc="Content of existing test files")
    code_context: str = dspy.InputField(desc="Relevant source code for context")
    test_stubs: str = dspy.OutputField(desc="pytest test stubs as Python code")


class TestGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.ChainOfThought(TestGeneratorSignature)

    def forward(
        self, requirements: str, existing_tests: str, code_context: str
    ) -> str:
        result = self.predict(
            requirements=requirements,
            existing_tests=existing_tests,
            code_context=code_context,
        )
        return result.test_stubs
