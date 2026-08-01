import tempfile
import unittest
from pathlib import Path

from ecount_credential_store import delete_api_key, load_api_key, save_api_key


class EcountCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "keys.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_saves_encrypted_key_and_restores_for_same_windows_user(self):
        save_api_key("JUNHO191", "secret-api-key", self.path)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-api-key", raw)
        self.assertEqual(load_api_key("junho191", self.path), "secret-api-key")

    def test_deletes_saved_key(self):
        save_api_key("JUNHO191", "secret-api-key", self.path)
        delete_api_key("JUNHO191", self.path)
        self.assertEqual(load_api_key("JUNHO191", self.path), "")


if __name__ == "__main__":
    unittest.main()
