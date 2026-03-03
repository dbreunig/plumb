import json
from unittest.mock import patch, MagicMock

from plumb.programs.spec_updater import WholeFileSpecUpdater, OutlineMerger


class TestWholeFileSpecUpdater:
    def test_parses_json_output(self):
        """The module should parse JSON strings from LLM into dicts."""
        mock_prediction = MagicMock()
        mock_prediction.section_updates_json = json.dumps([
            {"header": "## Auth", "content": "Updated auth section."}
        ])
        mock_prediction.new_sections_json = json.dumps([])

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            updater = WholeFileSpecUpdater()
            section_updates, new_sections = updater(
                spec_content="# Spec\n\n## Auth\n\nOld text.\n",
                decisions_text="1. Q: How to auth?\n   A: Use JWT.\n",
            )

        assert len(section_updates) == 1
        assert section_updates[0]["header"] == "## Auth"
        assert new_sections == []

    def test_handles_new_sections(self):
        mock_prediction = MagicMock()
        mock_prediction.section_updates_json = json.dumps([])
        mock_prediction.new_sections_json = json.dumps([
            {"header": "## Cache", "content": "Use Redis for caching."}
        ])

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            updater = WholeFileSpecUpdater()
            section_updates, new_sections = updater(
                spec_content="# Spec\n\n## Auth\n\nLogin.\n",
                decisions_text="1. Q: Caching?\n   A: Use Redis.\n",
            )

        assert section_updates == []
        assert len(new_sections) == 1
        assert new_sections[0]["header"] == "## Cache"


class TestOutlineMerger:
    def test_parses_outline(self):
        mock_prediction = MagicMock()
        mock_prediction.merged_outline = "# Title\n## Auth\n## Cache\n## API"

        with patch("plumb.programs.spec_updater.dspy.Predict") as MockPredict:
            mock_predict_instance = MagicMock(return_value=mock_prediction)
            MockPredict.return_value = mock_predict_instance

            merger = OutlineMerger()
            result = merger(
                current_outline="# Title\n## Auth\n## API",
                new_headers="## Cache",
            )

        assert result == ["# Title", "## Auth", "## Cache", "## API"]
