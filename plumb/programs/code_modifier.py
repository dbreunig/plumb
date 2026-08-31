from __future__ import annotations

import json
import re
from pathlib import Path

from dotenv import load_dotenv


class CodeModifier:
    """Modify staged code to satisfy a rejected decision.
    Calls litellm.completion directly (not DSPy) because code modification
    is inherently open-ended."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        completion_fn=None,
    ):
        load_dotenv(override=False)
        self.repo_root = repo_root
        self.completion_fn = completion_fn

    def _resolve_model(self) -> tuple[str, int]:
        """The (model, max_tokens) to use: the program_models["code_modifier"]
        override if configured, else the config-wide model."""
        from plumb.config import find_repo_root, load_config
        from plumb.programs import resolve_model

        repo_root = self.repo_root if self.repo_root is not None else find_repo_root()
        if repo_root is not None:
            cfg = load_config(repo_root)
            if cfg is not None:
                entry = cfg.program_models.get("code_modifier") or {}
                if entry.get("model"):
                    return entry["model"], entry.get("max_tokens", 16000)
        return resolve_model(repo_root), 16000

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

        completion_fn = self.completion_fn
        if completion_fn is None:
            import litellm  # lazy: importing litellm is slow

            completion_fn = litellm.completion

        model, max_tokens = self._resolve_model()
        response = completion_fn(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.choices[0].message.content or ""
        return self._parse_response(text)

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
