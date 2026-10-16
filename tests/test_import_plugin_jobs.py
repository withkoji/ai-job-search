import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from import_plugin_jobs import merge_jobs


class PluginImport(unittest.TestCase):
    def setUp(self):
        self.job = {'title': 'Coordinator', 'company': 'Example', 'url': 'https://example.com/jobs/123?source=indeed'}

    def test_preserves_rank_metadata_and_deduplicates_url_and_pair(self):
        original = {'seen': {'legacy': {**self.job, 'rank_score': 90, 'status': 'ranked'}}}
        before = copy.deepcopy(original)
        result, added = merge_jobs(original, [self.job, {**self.job, 'title': 'Other'}], 'indeed')
        self.assertEqual(result, original)
        self.assertEqual(original, before)
        self.assertEqual(added, [])

    def test_tracks_dedup_and_provenance_without_fabricated_fit(self):
        result, added = merge_jobs({'seen': {}}, [self.job, self.job], 'indeed')
        self.assertEqual(len(added), 1)
        entry = result['seen'][added[0]]
        self.assertEqual(entry['source'], 'plugin')
        self.assertEqual(entry['url'], self.job['url'])
        self.assertIsNone(entry['posted_date'])
        self.assertNotIn('fit', entry)
        self.assertEqual(merge_jobs({'seen': {}}, [self.job], 'indeed', {('example', 'coordinator')})[1], [])

    def test_invalid_batch_does_not_mutate_state(self):
        for invalid in ({**self.job, 'url': 'javascript:alert(1)'}, {**self.job, 'posted_date': 'yesterday'}, {'title': 'Missing'}):
            state = {'seen': {}}
            with self.assertRaises(ValueError):
                merge_jobs(state, [self.job, invalid], 'indeed')
            self.assertEqual(state, {'seen': {}})

    def test_preview_apply_and_reimport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'results.json'
            source.write_text(json.dumps([self.job]))
            command = [sys.executable, str(ROOT / 'tools/import_plugin_jobs.py'), str(source), '--provider', 'indeed', '--root', directory]
            subprocess.run(command, check=True, capture_output=True)
            state = root / 'job_scraper/seen_jobs.json'
            self.assertFalse(state.exists())
            subprocess.run(command + ['--apply'], check=True, capture_output=True)
            before = state.read_bytes()
            result = subprocess.run(command + ['--apply'], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)['added'], [])
            self.assertEqual(state.read_bytes(), before)
