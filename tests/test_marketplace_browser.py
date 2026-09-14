import tempfile
import unittest
from pathlib import Path

from marketplace_browser import load_browser_settings, save_browser_settings


class MarketplaceBrowserTests(unittest.TestCase):
    def test_settings_round_trip_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            self.assertEqual(load_browser_settings(path)["옥션"], "자동 선택")
            save_browser_settings(path, {"옥션": "Chrome", "지마켓": "Edge IE 모드"})
            loaded = load_browser_settings(path)
        self.assertEqual(loaded["옥션"], "Chrome")
        self.assertEqual(loaded["지마켓"], "Edge IE 모드")


if __name__ == "__main__":
    unittest.main()
