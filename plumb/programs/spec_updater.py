from __future__ import annotations

import json

import dspy


class WholeFileSpecUpdaterSignature(dspy.Signature):
    """Update a markdown spec to incorporate approved decisions.

    For EXISTING sections that need changes, return them in section_updates_json
    as a JSON array of {"header": "exact header line", "content": "new body"}.

    For BRAND NEW sections (not in the current spec), return them in
    new_sections_json as a JSON array of {"header": "## New Header", "content": "body"}.

    Rules:
    - Only include sections that need changes — omit unchanged sections
    - Use the EXACT header text from the spec for existing sections
    - Capture decisions as natural requirements — do not reference decisions
    - Preserve the spec's formatting style and voice
    - Return empty JSON arrays [] when there are no updates or new sections
    """

    spec_content: str = dspy.InputField(desc="Full markdown spec file")
    decisions_text: str = dspy.InputField(
        desc="Decisions as numbered list: 1. Question: ...\n   Decision: ..."
    )
    section_updates_json: str = dspy.OutputField(
        desc='JSON array of {"header": "## X", "content": "new body"} for existing sections'
    )
    new_sections_json: str = dspy.OutputField(
        desc='JSON array of {"header": "## X", "content": "body"} for new sections, or []'
    )


class WholeFileSpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(WholeFileSpecUpdaterSignature)

    def forward(
        self, spec_content: str, decisions_text: str
    ) -> tuple[list[dict], list[dict]]:
        result = self.predict(
            spec_content=spec_content, decisions_text=decisions_text
        )
        section_updates = json.loads(result.section_updates_json)
        new_sections = json.loads(result.new_sections_json)
        return section_updates, new_sections


class OutlineMergerSignature(dspy.Signature):
    """Given the current spec outline (headers only) and new section headers,
    return the complete merged outline with new headers placed at the most
    logical positions. Return all headers, one per line, preserving heading
    levels. Do not remove or rename any existing headers."""

    current_outline: str = dspy.InputField(
        desc="Current spec headers, one per line"
    )
    new_headers: str = dspy.InputField(
        desc="New section headers to place, one per line"
    )
    merged_outline: str = dspy.OutputField(
        desc="All headers in correct order, one per line"
    )


class OutlineMerger(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(OutlineMergerSignature)

    def forward(self, current_outline: str, new_headers: str) -> list[str]:
        result = self.predict(
            current_outline=current_outline, new_headers=new_headers
        )
        return [
            line.strip()
            for line in result.merged_outline.strip().split("\n")
            if line.strip()
        ]
