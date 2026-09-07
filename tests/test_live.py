"""Opt-in public API integration. Uses an isolated database and no YouTube writes."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import ytclaw as y
import ytclaw_ops as ops
import ytclaw_reports as reports


@unittest.skipUnless(os.environ.get('YTCLAW_LIVE_CHANNEL'), 'set YTCLAW_LIVE_CHANNEL for public API integration')
class PublicIntegration(unittest.TestCase):
    def test_metadata_sync_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'live.sqlite'; c = y.connect(db)
            try:
                result, code = ops.run_sync(c, db, os.environ['YTCLAW_LIVE_CHANNEL'])
                self.assertEqual(code, 0, result)
                self.assertGreater(result['stats_refreshed'], 0)
                row = c.execute('select metadata_json from metadata_versions order by version_id desc limit 1').fetchone()
                image = y.archive_thumbnail(c, json.loads(row[0]).get('thumbnails', {}))
                self.assertIsNotNone(image)
                self.assertGreater(c.execute('select length(data) from thumbnail_assets where sha256=?', (image['sha256'],)).fetchone()[0], 0)
                health = reports.coverage(c)[0]
                self.assertEqual(health['metadata_observed'], result['stats_refreshed'])
                self.assertEqual(health['latest_run']['status'], 'success')
                print(json.dumps({'live_channel': os.environ['YTCLAW_LIVE_CHANNEL'], 'videos_refreshed': result['stats_refreshed'],
                                  'quota_units': result['quota_units_this_run'], 'thumbnail_archived': True}))
            finally:
                c.close()


if __name__ == '__main__': unittest.main()
