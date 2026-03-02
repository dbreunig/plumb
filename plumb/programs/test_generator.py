from __future__ import annotations

import dspy


class TestGeneratorSignature(dspy.Signature):
    """Generate pytest test stubs for uncovered requirements.

    Rules:
    - One function per requirement, descriptive names (test_req_<id>_<description>)
    - Each function MUST start with a ``# plumb:req-XXXXXXXX`` comment linking
      it to the requirement it covers (use the exact requirement ID)
    - Stubs include '# TODO: implement' and pytest.skip()
    - Do not overwrite existing tests

    Example output::

        def test_req_abc12345_creates_config(self, tmp_repo):
            # plumb:req-abc12345
            # TODO: implement
            pytest.skip("Not implemented")
    """

    requirements: str = dspy.InputField(desc="Uncovered requirements as text")
    existing_tests: str = dspy.InputField(desc="Content of existing test files")
    code_context: str = dspy.InputField(desc="Relevant source code for context")
    test_stubs: str = dspy.OutputField(desc="pytest test stubs as Python code")


class TestGenerator(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(TestGeneratorSignature)

    def forward(
        self, requirements: str, existing_tests: str, code_context: str
    ) -> str:
        result = self.predict(
            requirements=requirements,
            existing_tests=existing_tests,
            code_context=code_context,
        )
        return result.test_stubs
