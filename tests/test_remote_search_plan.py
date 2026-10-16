import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from remote_search_plan import build_plan


class RemoteSearchPlan(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'config/us_remote_search.json').read_text())

    def test_all_queries_keep_us_remote_constraints(self):
        plan = build_plan(self.config)
        self.assertEqual(len({q['category'] for q in plan}), 10)
        for query in plan:
            self.assertEqual(query['country_code'], 'US')
            self.assertEqual(query['location'], 'United States')
            self.assertIs(query['remote_only'], True)
            self.assertTrue(query['search'])
            self.assertNotIn('link_id', query)

    def test_category_filter_and_unknown_category(self):
        self.assertTrue(all(q['category'] == 'human-resources' for q in build_plan(self.config, 'human-resources')))
        with self.assertRaises(ValueError):
            build_plan(self.config, 'unknown')

    def test_rejects_broadened_geography_or_remote_flag(self):
        for change in ({'country_code': 'CA'}, {'remote_only': False}):
            with self.assertRaises(ValueError):
                build_plan({**self.config, **change})
