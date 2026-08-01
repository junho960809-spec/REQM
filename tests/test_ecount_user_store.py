import tempfile
import unittest
from pathlib import Path

from ecount_user_store import delete_ecount_user, load_ecount_users, upsert_ecount_user


class EcountUserStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "users.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_adds_and_updates_user_case_insensitively(self):
        upsert_ecount_user({"user_id": "JUNHO191", "employee_code": "00210"}, self.path)
        upsert_ecount_user(
            {"user_id": "junho191", "employee_code": "00999", "display_name": "준호"}, self.path
        )
        users = load_ecount_users(self.path)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["employee_code"], "00999")
        self.assertEqual(users[0]["display_name"], "준호")

    def test_deletes_saved_user(self):
        upsert_ecount_user({"user_id": "JUNHO191", "employee_code": "00210"}, self.path)
        delete_ecount_user("junho191", self.path)
        self.assertEqual(load_ecount_users(self.path), [])


if __name__ == "__main__":
    unittest.main()
