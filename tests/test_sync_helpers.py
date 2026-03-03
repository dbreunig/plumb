from plumb.sync import extract_outline


class TestExtractOutline:
    def test_extracts_headers(self):
        content = "# Title\n\nIntro.\n\n## Auth\n\nLogin.\n\n### Tokens\n\nJWT.\n"
        assert extract_outline(content) == ["# Title", "## Auth", "### Tokens"]

    def test_empty_content(self):
        assert extract_outline("") == []

    def test_no_headers(self):
        assert extract_outline("Just some text.\nAnother line.") == []
