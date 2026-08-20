import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import types

sys.modules.setdefault("requests", types.SimpleNamespace(get=Mock()))
import trackr_common


class TrackrEmptyResponseTests(unittest.TestCase):
    @patch("trackr_common.requests.get")
    def test_empty_trackr_response_fails_before_csv_processing(self, mock_get):
        response = Mock()
        response.json.return_value = {"programmes": []}
        mock_get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "returned no programmes"):
            trackr_common.scrape_open_programmes({"region": "Hong Kong"})

        response.raise_for_status.assert_called_once_with()

    def test_write_csv_preserves_existing_file_when_offers_are_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "offers.csv"
            original_content = "Name,Offer URL\nExisting,https://example.com/job\n"
            csv_path.write_text(original_content, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                trackr_common.write_csv([], csv_path)

            self.assertEqual(csv_path.read_text(encoding="utf-8"), original_content)


if __name__ == "__main__":
    unittest.main()
