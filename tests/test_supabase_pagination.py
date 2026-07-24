from __future__ import annotations

import unittest

from main import fetch_all_rows


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.start = 0
        self.end = 0

    def select(self, _columns):
        return self

    def range(self, start, end):
        self.start, self.end = start, end
        return self

    def execute(self):
        return _Response(self.rows[self.start:self.end + 1])


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _Query(self.rows)


class SupabasePaginationTests(unittest.TestCase):
    def test_fetches_rows_beyond_first_page(self):
        expected = [{"id": index} for index in range(1005)]
        self.assertEqual(fetch_all_rows(_Client(expected), "items"), expected)


if __name__ == "__main__":
    unittest.main()
