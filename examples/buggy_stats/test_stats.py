import unittest
from stats import mean

class MeanTests(unittest.TestCase):
    def test_many(self):
        self.assertEqual(mean([2, 4, 6]), 4)
    def test_single(self):
        self.assertEqual(mean([7]), 7)
    def test_empty(self):
        with self.assertRaises(ValueError):
            mean([])

if __name__ == "__main__":
    unittest.main()
