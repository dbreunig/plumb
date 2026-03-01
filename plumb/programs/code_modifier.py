from __future__ import annotations

import json
import re

import anthropic


class CodeModifier:
    """Modify staged code to satisfy a rejected decision.
    Uses Anthropic API directly (not DSPy) because code modification
    is inherently open-ended."""

    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def modify(
        self,
        staged_diff: str,
        decision: str,
        rejection_reason: str,
        spec_content: str,
    ) -> dict[str, str]:
        """Returns dict mapping file paths to their modified contents."""
        prompt = f"""You are modifying staged code to satisfy a rejected decision.

## The Decision That Was Made
{decision}

## Why It Was Rejected
{rejection_reason}

## Current Spec
{spec_content}

## Staged Diff
{staged_diff}

## Instructions
Modify the staged code so that the rejected decision is reversed or corrected,
while keeping behavior consistent with the spec. Return ONLY a JSON object
mapping file paths to their complete modified file contents.

Return format:
```json
{{
  "path/to/file.py": "complete file contents here..."
}}
```"""

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        return self._parse_response(response.content[0].text)

    @staticmethod
    def _parse_response(text: str) -> dict[str, str]:
        """Extract file modifications from the LLM response."""
        # Try to find JSON block in response
        json_match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        # Try parsing entire response as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
