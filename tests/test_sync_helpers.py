from plumb.sync import extract_outline, apply_section_updates


class TestExtractOutline:
    def test_extracts_headers(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nLogin.\n\n### Tokens\n\nJWT.\n"
        assert extract_outline(content) == ["# Title", "## Auth", "### Tokens"]

    def test_empty_content(self):
        assert extract_outline("") == []

    def test_no_headers(self):
        assert extract_outline("Just some text.\nAnother line.") == []


class TestApplySectionUpdates:
    def test_replaces_matching_section(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nOld auth text.\n\n## API\n\nEndpoints.\n"
        updates = [{"header": "## Auth", "content": "New auth text with tokens.\n"}]
        result = apply_section_updates(content, updates)
        assert "New auth text with tokens." in result
        assert "Old auth text." not in result
        assert "Endpoints." in result  # untouched

    def test_no_updates(self):
        content = "# Title\n\nStuff.\n"
        result = apply_section_updates(content, [])
        assert result == content

    def test_normalized_header_match(self):
        """LLM might return slightly different whitespace."""
        content = "# Title\n\n##  Auth \n\nOld text.\n"
        updates = [{"header": "## Auth", "content": "New text.\n"}]
        result = apply_section_updates(content, updates)
        assert "New text." in result

    def test_multiple_updates(self):
        content = "# Title\n\nIntro.\n\n## A\n\nOld A.\n\n## B\n\nOld B.\n"
        updates = [
            {"header": "## A", "content": "New A.\n"},
            {"header": "## B", "content": "New B.\n"},
        ]
        result = apply_section_updates(content, updates)
        assert "New A." in result
        assert "New B." in result
        assert "Old A." not in result
        assert "Old B." not in result
