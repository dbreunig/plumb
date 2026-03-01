from pathlib import Path

import pytest

from plumb.ignore import parse_plumbignore, is_ignored, DEFAULT_PLUMBIGNORE


class TestParseNullCases:
    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_plumbignore(tmp_path) == []

    def test_empty_file_returns_empty(self, tmp_path):
        (tmp_path / ".plumbignore").write_text("")
        assert parse_plumbignore(tmp_path) == []

    def test_comments_and_blanks_skipped(self, tmp_path):
        (tmp_path / ".plumbignore").write_text("# comment\n\n  # indented\n  \n")
        assert parse_plumbignore(tmp_path) == []


class TestParsePlumbignore:
    def test_parses_patterns(self, tmp_path):
        (tmp_path / ".plumbignore").write_text("README.md\n*.txt\ndocs/\n")
        patterns = parse_plumbignore(tmp_path)
        assert patterns == ["README.md", "*.txt", "docs/"]

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / ".plumbignore").write_text("  README.md  \n  docs/  \n")
        patterns = parse_plumbignore(tmp_path)
        assert patterns == ["README.md", "docs/"]

    def test_mixed_comments_and_patterns(self, tmp_path):
        content = "# header\nREADME.md\n\n# another\ndocs/\n"
        (tmp_path / ".plumbignore").write_text(content)
        patterns = parse_plumbignore(tmp_path)
        assert patterns == ["README.md", "docs/"]


class TestIsIgnoredExactMatch:
    def test_exact_match(self):
        assert is_ignored("README.md", ["README.md"]) is True

    def test_no_match(self):
        assert is_ignored("app.py", ["README.md"]) is False

    def test_exact_match_with_path(self):
        assert is_ignored("Makefile", ["Makefile"]) is True


class TestIsIgnoredGlob:
    def test_glob_star_extension(self):
        assert is_ignored("notes.txt", ["*.txt"]) is True

    def test_glob_basename_in_subdir(self):
        assert is_ignored("src/notes.txt", ["*.txt"]) is True

    def test_glob_no_match(self):
        assert is_ignored("app.py", ["*.txt"]) is False

    def test_glob_prefix(self):
        assert is_ignored("docker-compose.yml", ["docker-compose*"]) is True
        assert is_ignored("docker-compose.override.yml", ["docker-compose*"]) is True

    def test_license_glob(self):
        assert is_ignored("LICENSE.md", ["LICENSE.*"]) is True
        assert is_ignored("LICENSE.txt", ["LICENSE.*"]) is True


class TestIsIgnoredDirectoryPrefix:
    def test_directory_match(self):
        assert is_ignored("docs/guide.md", ["docs/"]) is True

    def test_nested_directory_match(self):
        assert is_ignored("docs/api/ref.md", ["docs/"]) is True

    def test_directory_no_match(self):
        assert is_ignored("src/docs_util.py", ["docs/"]) is False

    def test_bare_dirname_matches(self):
        # "docs" (without trailing /) matches as the dir name itself
        assert is_ignored("docs", ["docs/"]) is True

    def test_github_dir(self):
        assert is_ignored(".github/workflows/ci.yml", [".github/"]) is True


class TestIsIgnoredMultiplePatterns:
    def test_matches_any(self):
        patterns = ["README.md", "*.txt", "docs/"]
        assert is_ignored("README.md", patterns) is True
        assert is_ignored("notes.txt", patterns) is True
        assert is_ignored("docs/x.md", patterns) is True
        assert is_ignored("app.py", patterns) is False

    def test_empty_patterns(self):
        assert is_ignored("anything.py", []) is False


class TestDefaultPlumbignore:
    def test_default_is_nonempty_string(self):
        assert isinstance(DEFAULT_PLUMBIGNORE, str)
        assert len(DEFAULT_PLUMBIGNORE) > 0

    def test_default_parseable(self, tmp_path):
        (tmp_path / ".plumbignore").write_text(DEFAULT_PLUMBIGNORE)
        patterns = parse_plumbignore(tmp_path)
        assert "README.md" in patterns
        assert "docs/" in patterns
        assert "LICENSE" in patterns
