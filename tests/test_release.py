import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import threading
import zipfile

import ytclaw as y
import ytclaw_ops as ops
import ytclaw_reports as reports
import ytclaw_analytics as owner
from ytclaw_web import DeskServer


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'data.sqlite'
        self.c = y.connect(self.db)
        self.c.execute('insert into channels values(?,?,?,?,?,?)', ('UC1', '@one', 'Channel One', 'UP1', y.now(), y.now()))
        self.c.execute('insert into channels values(?,?,?,?,?,?)', ('UC2', '@two', 'Channel Two', 'UP2', y.now(), y.now()))
        self.ch = {'channel_id':'UC1', 'uploads':'UP1'}
        self.c.commit()

    def tearDown(self):
        self.c.close(); self.temp.cleanup()

    def put(self, vid='v1', title='First title', cid='UC1', when='2026-09-01T00:00:00Z', **fields):
        y.upsert_video(self.c, {'video_id':vid, 'channel_id':cid, 'title':title,
                               'description':'A useful setup', 'published_at':'2026-08-01T00:00:00Z', **fields}, 'api', when)
        self.c.commit()


class ReliabilityTests(DatabaseTest):
    def test_sql_rejects_cte_write_pragma_and_attach(self):
        self.put()
        for query in ["WITH x AS (SELECT 1) DELETE FROM videos", "PRAGMA query_only=off", "ATTACH ':memory:' AS other", "SELECT load_extension('bad')"]:
            with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError): y.readonly_sql(self.c, query)
        self.assertEqual(y.readonly_sql(self.c, 'WITH x AS (SELECT title FROM videos) SELECT * FROM x')[0]['title'], 'First title')
        self.assertEqual(y.readonly_sql(self.c, "SELECT count(*) n FROM videos_fts WHERE videos_fts MATCH 'setup'")[0]['n'], 1)

    def test_stats_reversion_and_channel_snapshots(self):
        self.put(views=100)
        self.put(views=80, when='2026-09-02T00:00:00Z')
        self.put(views=100, when='2026-09-03T00:00:00Z')
        self.assertEqual([r['views'] for r in y.video(self.c,'v1')['stats_history']], [100,80,100])
        for cid in ['UC1','UC2']:
            self.c.execute('insert into channel_snapshots values(?,?,?,?,?,?)', ('same',cid,1,2,3,'api'))
        self.assertEqual(self.c.execute('select count(*) from channel_snapshots').fetchone()[0], 2)

    def test_sync_lock_prevents_overlapping_runs(self):
        with ops.sync_lock(self.db):
            with self.assertRaisesRegex(ValueError, 'another sync'):
                ops.run_sync(self.c, self.db, '@one')
        self.assertEqual(self.c.execute('select count(*) from sync_runs').fetchone()[0], 0)

    def test_old_database_migration_preserves_channel_and_stats(self):
        self.c.executescript("""drop table channel_snapshots;
          create table channel_snapshots(observed_at text primary key,channel_id text,subscribers integer,total_views integer,video_count integer,source text);
          insert into channel_snapshots values('2026-01-01','UC1',1,2,3,'api');
          delete from sync_cache where cache_key='migration:observations-v1';
          insert into stats_snapshots values('old','hash','2026-01-01','2026-01-03',10,1,1,'api');""")
        self.c.close();self.c=y.connect(self.db)
        self.assertEqual(self.c.execute('select subscribers from channel_snapshots').fetchone()[0], 1)
        self.assertEqual(self.c.execute("select source from stats_observations where video_id='old'").fetchone()[0], 'legacy:api')
        self.c.close();self.c=y.connect(self.db)
        self.assertEqual(self.c.execute("select count(*) from stats_observations where video_id='old'").fetchone()[0], 1)

    def test_discovery_ids_survive_kill_and_resume(self):
        class Interrupted:
            def get(_, resource, **kw):
                if kw.get('pageToken') == 'next': raise RuntimeError('killed')
                return {'items':[{'contentDetails':{'videoId':'v1'}}], 'nextPageToken':'next'}
        with self.assertRaises(RuntimeError): y.sync_videos(self.c, Interrupted(), self.ch, True)
        with contextlib.closing(sqlite3.connect(self.db)) as check:
            self.assertEqual(check.execute('select video_id from pending_videos').fetchone()[0], 'v1')
            self.assertEqual(json.loads(check.execute("select value_json from sync_cache where cache_key='uploads:UC1:cursor'").fetchone()[0])['token'], 'next')
        calls = []
        class Resumed:
            def get(_, resource, **kw):
                calls.append((resource,kw))
                if resource == 'playlistItems': return {'items':[{'contentDetails':{'videoId':'v2'}}]}
                return {'items':[{'id':vid,'snippet':{'title':vid},'statistics':{'viewCount':'1'}} for vid in kw['id'].split(',')]}
        y.sync_videos(self.c, Resumed(), self.ch, True)
        self.assertEqual(calls[0][1]['pageToken'], 'next')
        self.assertEqual({r[0] for r in self.c.execute('select video_id from videos')}, {'v1','v2'})
        self.assertEqual(self.c.execute('select count(*) from pending_videos').fetchone()[0],0)

    def test_completed_metadata_batch_is_durable(self):
        ids=[f'v{i:03}' for i in range(51)]
        class Interrupted:
            def get(_, resource, **kw):
                if resource=='playlistItems': return {'items':[{'contentDetails':{'videoId':v}} for v in ids]}
                if kw['id']==ids[-1]: raise RuntimeError('kill')
                return {'items':[{'id':v,'snippet':{'title':v}} for v in kw['id'].split(',')]}
        with self.assertRaises(RuntimeError): y.sync_videos(self.c, Interrupted(), self.ch, True)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('select count(*) from videos').fetchone()[0],50)
            self.assertEqual(c.execute('select count(*) from pending_videos').fetchone()[0],1)

    def test_comments_all_replies_and_prune_after_complete(self):
        self.put(comments=4)
        y.upsert_comment(self.c,'v1',{'id':'deleted','text':'Old comment?'})
        snippet=lambda text:{'textDisplay':text,'authorDisplayName':'viewer','publishedAt':'2026-09-01T00:00:00Z'}
        calls=[]
        class Comments:
            def get(_, resource, **kw):
                calls.append((resource,kw))
                if resource=='commentThreads':
                    return {'items':[{'snippet':{'topLevelComment':{'id':'top','snippet':snippet('Question?')},'totalReplyCount':3},
                                      'replies':{'comments':[{'id':'r1','snippet':snippet('One')}]}}]}
                if not kw.get('pageToken'): return {'items':[{'id':'r1','snippet':snippet('One')},{'id':'r2','snippet':snippet('Two')}], 'nextPageToken':'rnext'}
                return {'items':[{'id':'r3','snippet':snippet('Three')}]}
        y.sync_comments(self.c, Comments(), self.ch, 7, 200)
        self.assertEqual({r[0] for r in self.c.execute('select comment_id from comments')},{'top','r1','r2','r3'})
        self.assertEqual(calls[-1][1]['pageToken'],'rnext')
        self.assertIsNotNone(self.c.execute('select comments_complete_at from collection_status').fetchone()[0])
        self.assertEqual(reports.questions(self.c),[])

    def test_failed_comment_scan_not_marked_complete(self):
        self.put(comments=1)
        class Broken:
            def get(*args,**kwargs): raise y.ApiError(403,'commentThreads','commentsDisabled')
        result=y.sync_comments(self.c, Broken(), self.ch, 7, 20)
        self.assertEqual(result['comment_errors'],1)
        self.assertIsNone(self.c.execute('select comments_complete_at from collection_status').fetchone()[0])

    def test_retry_counts_failed_calls_without_key_leak(self):
        response=io.BytesIO(b'{"items":[]}')
        error=urllib.error.HTTPError('https://example.test?key=secret',503,'unavailable',{},io.BytesIO(b'secret'))
        client=y.YT(self.c,'secret')
        with patch('urllib.request.urlopen',side_effect=[error,response]), patch('time.sleep'):
            self.assertEqual(client.get('videos',id='v1'),{'items':[]})
        self.assertEqual(client.used,2)
        self.assertEqual(y.cache_get(self.c,'quota:'+y.today())['calls'],2)

    def test_run_log_failure_and_stale_run_recovery(self):
        self.c.execute("insert into sync_runs(handle,status) values('@one','running')"); self.c.commit()
        with patch('ytclaw.api_key',side_effect=SystemExit('no API key')):
            out,code=ops.run_sync(self.c,self.db,'@one')
        self.assertEqual(code,1); self.assertEqual(out['status'],'failed')
        self.assertEqual([r[0] for r in self.c.execute('select status from sync_runs order by run_id')],['interrupted','failed'])


class ReportsTests(DatabaseTest):
    def test_channel_and_publication_filters_and_context(self):
        self.put(); self.put('v2',cid='UC2')
        y.replace_transcript(self.c,'v1',[{'start':0,'text':'Before'},{'start':3,'text':'Useful setup'},{'start':8,'text':'After'}])
        results=reports.search(self.c,'setup',channel='@one')
        self.assertTrue(results);self.assertEqual({r['video_id'] for r in results},{'v1'})
        transcript=next(r for r in results if r['kind']=='transcript')
        self.assertEqual(len(transcript['context']),3);self.assertIn('&t=3s',transcript['url'])
        self.assertEqual(reports.search(self.c,'setup',channel='@one',since='2026-09-01'),[])
        with self.assertRaises(ValueError): reports.top(self.c,channel='@missing')

    def test_until_date_includes_whole_day(self):
        self.put(when='2026-09-01T23:30:00Z')
        self.assertEqual(len(reports.changes(self.c,until='2026-09-01')),1)
        self.assertEqual(len(reports.changes(self.c,until='2026-09-01T00:00:00Z')),0)

    def test_growth_uses_boundaries_not_max_minus_min(self):
        self.put(views=100);self.put(views=80,when='2026-09-02T00:00:00Z');self.put(views=90,when='2026-09-03T00:00:00Z')
        result=reports.growth(self.c,'@one','2026-09-01','2026-09-03')[0]
        self.assertEqual(result['views_gained'],-10);self.assertEqual(result['views_per_day'],-5)
        self.assertEqual(result['from_observed_at'],'2026-09-01T00:00:00Z')

    def test_change_window_and_coverage(self):
        self.put();self.put(title='Changed',when='2026-09-03T00:00:00Z')
        result=reports.changes(self.c,'@one','2026-09-02')
        self.assertEqual(len(result),1);self.assertEqual(result[0]['changes']['title']['before'],'First title')
        cs=reports.coverage(self.c,'@one')[0]
        self.assertEqual(cs['videos'],1);self.assertEqual(cs['with_transcripts'],0)

    def test_project_compares_only_mapped_fields_and_api(self):
        self.put()
        title=self.root/'title.txt';title.write_text('Expected title\n')
        manifest=self.root/'project.json';manifest.write_text(json.dumps({'title_file':'title.txt'}))
        ops.link_project(self.c,'v1',manifest)
        result=ops.project_drift(self.c,'v1');self.assertEqual(set(result['changes']),{'title'})
        y.upsert_video(self.c,{'video_id':'v1','title':'Expected title'},'yaml');self.c.commit()
        self.assertTrue(ops.project_drift(self.c,'v1')['drifted'])
        title.write_text('First title\n');self.assertFalse(ops.project_drift(self.c,'v1')['drifted'])


class ArchiveTests(DatabaseTest):
    def test_backup_restore_and_no_overwrite(self):
        self.put()
        backup=self.root/'backup.sqlite';ops.backup(self.c,backup)
        with self.assertRaises(FileExistsError):ops.backup(self.c,backup)
        target=self.root/'restored.sqlite';result=ops.restore(backup,target)
        self.assertEqual(result['integrity'],'ok')
        with contextlib.closing(y.connect(target)) as restored:
            self.assertEqual(y.video(restored,'v1')['title'],'First title')
        with self.assertRaises(ValueError):ops.restore(backup,target)

    def test_portable_export_restores_images_history_and_search(self):
        self.put()
        self.c.execute('insert into thumbnail_assets values(?,?,?)',('hash','image/png',b'image bytes'));self.c.commit()
        archive=self.root/'export.zip';ops.export_archive(self.c,archive)
        with zipfile.ZipFile(archive) as z:
            self.assertIn('tables/metadata_versions.jsonl',z.namelist())
            self.assertEqual(json.loads(z.read('manifest.json'))['tables']['videos']['rows'],1)
        restored=self.root/'restored.sqlite';ops.restore(archive,restored)
        with contextlib.closing(y.connect(restored)) as c:
            self.assertTrue(reports.search(c,'setup'))
            self.assertEqual(c.execute('select data from thumbnail_assets').fetchone()[0],b'image bytes')

    def test_tampered_archive_rejected(self):
        self.put();archive=self.root/'export.zip';ops.export_archive(self.c,archive)
        bad=self.root/'bad.zip'
        with zipfile.ZipFile(archive) as src,zipfile.ZipFile(bad,'w') as dst:
            for name in src.namelist():dst.writestr(name,b'broken' if name=='database.sqlite' else src.read(name))
        with self.assertRaisesRegex(ValueError,'checksum'):ops.restore(bad,self.root/'new.sqlite')
        self.assertFalse((self.root/'new.sqlite').exists())

    def test_mac_timer_install_and_remove_are_scoped(self):
        with patch('pathlib.Path.home',return_value=self.root), patch('ytclaw.HOME',self.root/'config'), patch('sys.platform','darwin'), patch('subprocess.run') as run:
            result=ops.schedule(self.db,'install')
            import plistlib
            path=Path(result['timer']); data=plistlib.loads(path.read_bytes())
            self.assertEqual(data['ProgramArguments'][-3:],['watch','run','--once'])
            self.assertIn(str(self.db.resolve()),data['ProgramArguments'])
            self.assertEqual(data['StartInterval'],300)
            ops.schedule(self.db,'remove');self.assertFalse(path.exists())
            self.assertTrue(any(call.args[0][1]=='bootstrap' for call in run.call_args_list))

    def test_watch_run_only_due_jobs(self):
        ops.watch_add(self.c,'@one',24,comments=True)
        with patch('ytclaw_ops.run_sync',return_value=({'status':'success'},0)) as sync,contextlib.redirect_stdout(io.StringIO()):
            ops.watch_run(self.c,self.db,once=True);ops.watch_run(self.c,self.db,once=True)
        self.assertEqual(sync.call_count,1)


class AnalyticsTests(DatabaseTest):
    @unittest.skipUnless(importlib.util.find_spec('google_auth_oauthlib'), 'optional owner dependency not installed')
    def test_oauth_uses_readonly_pkce_and_private_token_file(self):
        client_file=self.root/'client.json';client_file.write_text(json.dumps({'installed':{'client_id':'fixture'}}))
        from unittest.mock import Mock
        flow=Mock();flow.run_local_server.return_value.to_json.return_value=json.dumps({'token':'fixture-token','scopes':[owner.SCOPE]})
        with patch('ytclaw.HOME',self.root/'config'),patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config',return_value=flow) as create:
            result=owner.auth_login(client_file)
            self.assertTrue(result['authenticated'])
            self.assertTrue(create.call_args.kwargs['autogenerate_code_verifier'])
            self.assertEqual(create.call_args.args[1],[owner.SCOPE])
            self.assertEqual(flow.run_local_server.call_args.kwargs['host'],'127.0.0.1')
            token=self.root/'config/owner-token.json'
            self.assertEqual(token.stat().st_mode & 0o777,0o600)
            self.assertNotIn('token',owner.auth_status())

    def test_api_pagination_and_idempotent_resume(self):
        self.put()
        class Client:
            calls=0
            def get(self,**params):
                self.calls+=1
                assert params['ids']=='channel==UC1'
                return {'columnHeaders':[{'name':'day'},{'name':'views'}], 'rows':[['2026-09-01',1]]*200 if params['startIndex']==1 else [['2026-09-02',2]]}
        client=Client()
        result=owner.sync(self.c,'@one','2026-09-01','2026-09-02',kind='daily',client=client)
        self.assertEqual(result['api_calls'],2);self.assertEqual(len(owner.show(self.c,'v1')[0]['rows']),201)
        again=owner.sync(self.c,'@one','2026-09-01','2026-09-02',kind='daily',client=client)
        self.assertEqual(again['reports_saved'],0)

    def test_csv_units_validation_and_atomic_failure(self):
        self.put();path=self.root/'reach.csv'
        path.write_text('Date,Impressions,Impressions click-through rate (%)\n2026-09-01,"1,000",4.5%\n')
        self.assertEqual(owner.import_csv(self.c,'v1',path)['rows'],1)
        self.assertEqual(owner.show(self.c,'v1')[0]['rows'][0],['2026-09-01',1000.0,4.5])
        path.write_text('Date,Impressions,Impressions click-through rate (%)\n2026-09-02,100,300\n')
        with self.assertRaises(ValueError):owner.import_csv(self.c,'v1',path)
        self.assertEqual(len(owner.show(self.c,'v1')),1)

    def test_compare_excludes_change_day_and_marks_missing(self):
        self.put(when='2026-09-03T12:00:00Z')
        version=y.metadata_history(self.c,'v1')[0]['version_id']
        owner.save_report(self.c,'UC1','v1','daily','2026-09-01','2026-09-05',['day','views','estimatedMinutesWatched'],
                          [['2026-09-02',10,20],['2026-09-03',999,999],['2026-09-04',30,40]],'analytics_api')
        result=owner.compare(self.c,'v1',version,2)
        self.assertEqual(result['before']['views'],10);self.assertEqual(result['after']['views'],30)
        self.assertEqual(result['after']['missing_days'],['2026-09-05'])


class WebTests(DatabaseTest):
    def setUp(self):
        super().setUp();self.put()
        self.server=DeskServer(self.db,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();super().tearDown()

    def test_routes_and_report_download(self):
        for route in ['/','/app.js','/styles.css','/api/overview','/api/video?id=v1','/api/search?q=setup','/api/report.md']:
            with self.subTest(route=route),urllib.request.urlopen(self.url+route) as response:
                self.assertEqual(response.status,200)
                self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])

    def test_mutations_require_local_token_and_host(self):
        body=json.dumps({'video_id':'v1'}).encode()
        req=urllib.request.Request(self.url+'/api/baseline',data=body,headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        self.assertEqual(error.exception.code,403); error.exception.close()
        req.add_header('X-Ytclaw-Token',self.server.token)
        with urllib.request.urlopen(req) as response:self.assertEqual(json.load(response)['video_id'],'v1')
        req.add_header('Origin','https://other.test')
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        error.exception.close()
        malicious=urllib.request.Request(self.url+'/api/overview',headers={'Host':'attacker.test'})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(malicious)
        error.exception.close()


if __name__=='__main__':unittest.main()
