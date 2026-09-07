"""Synthetic offline channel for onboarding and repeatable browser tests."""
import hashlib
import json
from pathlib import Path
import struct
import zlib

import ytclaw as y


def thumbnail(seed):
    width, height = 480, 270
    palettes = [((22, 51, 48), (210, 241, 121)), ((239, 101, 63), (255, 238, 207)), ((39, 53, 98), (180, 207, 254))]
    background, accent = palettes[seed % 3]
    data = bytearray()
    for row in range(height):
        data.append(0)
        for col in range(width):
            circle = (col-340)**2 + (row-135)**2 < (75+seed*3)**2
            stripe = 32 < col < 215 and any(top < row < top+15 for top in (75, 107, 139, 184))
            data.extend(accent if circle or stripe else background)
    def chunk(kind, payload):
        return struct.pack('!I', len(payload)) + kind + payload + struct.pack('!I', zlib.crc32(kind+payload) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(data))) + chunk(b'IEND', b'')


def create(db):
    if Path(db).exists(): raise ValueError('demo needs a new --db path; existing data is never replaced')
    c = y.connect(db)
    try:
        c.execute('insert into channels values(?,?,?,?,?,?)', ('DEMO', '@Demo', 'The Field Notes Channel', 'DEMO_UPLOADS', y.days_ago(14), y.now()))
        titles = [('What my desk setup gets wrong', 'A smaller desk, a better workday'),
                  ('Learning to cook without a recipe', 'Learning to cook without a recipe'),
                  ('A weekend with no notifications', 'A weekend with no notifications')]
        for i, (before, after) in enumerate(titles):
            vid = f'demo_video{i+1}'
            for days, title, views, variant in [(8, before, 2400+i*1200, i), (2, after, 5900+i*1500, i+1), (0, after, 6800+i*1700, i+1)]:
                image = thumbnail(variant); sha = hashlib.sha256(image).hexdigest()
                c.execute('insert or ignore into thumbnail_assets values(?,?,?)', (sha, 'image/png', image))
                y.upsert_video(c, {'video_id': vid, 'channel_id': 'DEMO', 'title': title,
                                  'description': 'Synthetic sample video for exploring ytclaw. This is not real channel data.',
                                  'tags': ['field notes', 'experiments'], 'published_at': y.days_ago(14+i), 'duration': 'PT8M30S',
                                  'views': views, 'likes': 120+i*30, 'comments': 2,
                                  'thumbnail_asset': {'sha256': sha, 'content_type': 'image/png', 'url': 'demo'}}, 'demo', y.days_ago(days))
            y.replace_transcript(c, vid, [{'start': 0, 'duration': 5, 'text': 'This week I tried a small experiment.'},
                                          {'start': 5, 'duration': 8, 'text': 'The simplest setup turned out to be the most useful.'},
                                          {'start': 13, 'duration': 10, 'text': 'Here is what changed, what failed, and what I would try next.'}])
            y.upsert_comment(c, vid, {'id': f'demo-question-{i}', 'author': 'Sample viewer', 'text': ['Could you share the desk measurements?', 'Can you show a beginner version of this recipe?', 'How did you handle messages from family?'][i], 'likes': 7+i, 'published_at': y.days_ago(1)})
            c.execute('insert into collection_status values(?,?,?,?,?,?)', (vid, y.now(), y.now(), None, y.now(), None))
        c.execute("insert into sync_runs(handle,channel_id,started_at,finished_at,status,summary_json) values(?,?,?,?,?,?)",
                  ('@Demo', 'DEMO', y.now(), y.now(), 'success', json.dumps({'synthetic': True})))
        y.cache_set(c, 'demo', True); c.commit()
    finally:
        c.close()
    return {'db': str(db), 'synthetic': True, 'next': f'ytclaw --db {db} serve'}
