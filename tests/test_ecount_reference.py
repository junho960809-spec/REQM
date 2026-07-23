import unittest

from ecount_reference import load_ecount_items


class EcountReferenceTests(unittest.TestCase):
    def test_reference_file_contains_expected_items(self):
        items = load_ecount_items()
        by_code = {str(row["item_code"]): str(row["standard_name"]) for row in items}
        self.assertEqual(len(items), 169)
        self.assertEqual(
            by_code["[RQM]-QP1000C1-BK"],
            "[리큐엠] 보조배터리 QP1000C1 블랙",
        )


if __name__ == "__main__":
    unittest.main()
