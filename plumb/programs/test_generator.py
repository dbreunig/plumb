from __future__ import annotations

import dspy


class TestGeneratorSignature(dspy.Signature):
    """Generate runnable pytest tests for uncovered requirements.

    Rules:
    - One function per requirement, descriptive names (test_req_<id>_<description>)
    - Each function MUST start with a ``# plumb:req-XXXXXXXX`` comment linking
      it to the requirement it covers (use the exact requirement ID)
    - Tests MUST contain real assertions against the actual code under test
    - Import and call real functions/classes from the codebase
    - Use fixtures (tmp_path, monkeypatch, etc.) as needed for isolation
    - Do NOT use pytest.skip(), ``# TODO: implement``, or empty test bodies
    - Do not overwrite existing tests

    Example output::

        def test_req_abc12345_creates_config(tmp_path):
            # plumb:req-abc12345
            from plumb.config import load_config
            config_path = tmp_path / ".plumb" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"spec_files": ["spec.md"]}')
            config = load_config(tmp_path)
            assert config.spec_files == ["spec.md"]
    """

    requirements: str = dspy.InputField(desc="Uncovered requirements as text")
    existing_tests: str = dspy.InputField(desc="Content of existing test files")
    code_context: str = dspy.InputField(desc="Relevant source code for context")
    test_code: str = dspy.OutputField(desc="Runnable pytest test code with real assertions")


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
        return result.test_code
