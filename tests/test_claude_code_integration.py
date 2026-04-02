"""Integration test for ClaudeCodeLM with a real claude -p call.

Marked slow — skipped by default. Run with: pytest -m slow
Requires the claude CLI to be installed and authenticated.
"""

import json
import shutil

import dspy
import pytest
from dspy.adapters import XMLAdapter

from plumb.programs.claude_code_lm import ClaudeCodeLM, find_claude_cli

needs_claude_cli = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not installed",
)


@pytest.mark.slow
@needs_claude_cli
def test_claude_code_lm_parse_spec_single_file():
    """End-to-end: parse a tiny spec through ClaudeCodeLM → DSPy RequirementParser."""
    from plumb.programs.requirement_parser import RequirementParser

    lm = ClaudeCodeLM(model="sonnet", max_tokens=4000, timeout=60)
    dspy.configure(lm=lm, adapter=XMLAdapter())

    parser = RequirementParser()

    spec = """\
# Widget API

## Requirements

The system must accept a widget name as a string.
The system must return a 400 error if the name is empty.
"""

    parsed = parser(markdown=spec)
    assert len(parsed) >= 2, f"Expected at least 2 requirements, got {len(parsed)}"

    texts = [r.text.lower() for r in parsed]
    assert any("name" in t for t in texts), f"No requirement mentions 'name': {texts}"


@pytest.mark.slow
@needs_claude_cli
def test_claude_code_lm_raw_call():
    """Smoke test: ClaudeCodeLM returns a non-empty response for a simple prompt."""
    lm = ClaudeCodeLM(model="sonnet", max_tokens=100, timeout=30)

    response = lm("Reply with only the word: hello")
    assert response, "Got empty response from claude CLI"
    assert isinstance(response, list)
    assert len(response) > 0
    assert "hello" in response[0].lower()
