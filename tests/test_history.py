import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ytclaw as y


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.sqlite'
        self.c = y.connect(self.db)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def put(self, title, **extra):
        y.upsert_video(self.c, dict(video_id='v', title=title, **extra), 'api')

    def test_changes_reverts_and_baseline(self):
        self.put('A', tags=['two', 'one'], description='original')
        y.baseline(self.c, 'v')
        self.put('A', tags=['one', 'two'], views=100)
        self.assertEqual(len(y.metadata_history(self.c, 'v')), 1)
        self.put('B', description='')
        self.assertTrue(y.drift(self.c, 'v')['drifted'])
        self.assertEqual(y.drift(self.c, 'v')['changes']['title'], {'before': 'A', 'after': 'B'})
        self.put('A', description='original')
        self.assertEqual(len(y.metadata_history(self.c, 'v')), 3)
        self.assertFalse(y.drift(self.c, 'v')['drifted'])
        self.assertEqual(y.video(self.c, 'v')['tags'], ['one', 'two'])

    def test_migration_preserves_old_copy_once(self):
        self.put('old')
        self.c.execute('delete from metadata_versions')
        self.c.commit(); self.c.close()
        self.c = y.connect(self.db)
        self.put('new')
        self.c.commit(); self.c.close()
        self.c = y.connect(self.db)
        versions = y.metadata_history(self.c, 'v')
        self.assertEqual([v['metadata']['title'] for v in versions], ['old', 'new'])
        self.assertEqual(versions[0]['source'], 'migration')

    def test_thumbnail_same_url_changed_bytes(self):
        from io import BytesIO
        thumbs = {'high': {'url': 'https://i.ytimg.com/vi/v/hqdefault.jpg', 'width': 480}}
        hashes = []
        for data in [b'first', b'first', b'second']:
            response = BytesIO(data); response.headers = {'Content-Type': 'image/jpeg'}
            with patch('urllib.request.urlopen', return_value=response):
                asset = y.archive_thumbnail(self.c, thumbs)
            hashes.append(asset['sha256'])
            self.put('A', thumbnails=thumbs, thumbnail_asset=asset)
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[1], hashes[2])
        self.assertEqual(len(y.metadata_history(self.c, 'v')), 2)
        self.assertEqual(self.c.execute('select count(*) from thumbnail_assets').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
