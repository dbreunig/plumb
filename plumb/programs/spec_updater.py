from __future__ import annotations

import json

import dspy


class SpecUpdaterSignature(dspy.Signature):
    """Update a spec markdown section to incorporate an approved decision.
    The result of the decision should be captured as a natural requirement.
    Do not reference the decision itself. Preserve existing formatting."""

    spec_section: str = dspy.InputField(desc="Current markdown section of the spec")
    decision: str = dspy.InputField(desc="The approved decision text")
    question: str = dspy.InputField(desc="The question the decision answers")
    updated_section: str = dspy.OutputField(desc="Updated markdown for the section")


class SpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(SpecUpdaterSignature)

    def forward(self, spec_section: str, decision: str, question: str) -> str:
        result = self.predict(
            spec_section=spec_section, decision=decision, question=question
        )
        return result.updated_section


class BatchSpecUpdaterSignature(dspy.Signature):
    """Update a spec markdown section to incorporate multiple approved decisions.
    Each decision should be captured as a natural requirement.
    Do not reference the decisions themselves. Preserve existing formatting."""

    spec_section: str = dspy.InputField(desc="Current markdown section of the spec")
    decisions_text: str = dspy.InputField(
        desc="Multiple decisions formatted as numbered list with questions and answers"
    )
    updated_section: str = dspy.OutputField(desc="Updated markdown for the section")


class BatchSpecUpdater(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(BatchSpecUpdaterSignature)

    def forward(self, spec_section: str, decisions_text: str) -> str:
        result = self.predict(
            spec_section=spec_section, decisions_text=decisions_text
        )
        return result.updated_section


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
