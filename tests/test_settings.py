from contextlib import closing
from datetime import datetime,timezone,timedelta
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
import collector
import dashboard
from preferences import Preferences
from settings_api import export_query,csv_lines
from tests.test_collector import paid


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'config.json').write_text(json.dumps({'channels':['@one']}))
        self.store=collector.Store(self.root/'superchats.sqlite3',self.root/'chat-messages.sqlite3')
        self.store.execute('INSERT INTO channels VALUES (?,?)',('@one','c1'))
        self.prefs=Preferences(self.root)
        self.broadcast={'video_id':'v1','channel_id':'c1','chat_id':'chat'}
        self.text={'id':'normal','snippet':{'type':'textMessageEvent','publishedAt':'2026-11-18T12:00:00Z',
             'textMessageDetails':{'messageText':'=formula'}},'authorDetails':{'displayName':'@Viewer'}}

    def tearDown(self): self.temp.cleanup()

    def update(self,**flags):
        return self.prefs.mutate('update',{'channel_id':'c1','enabled':True,'record_chat':True,'record_paid':True,**flags})

    def test_migration_is_once_and_removed_streamers_do_not_return(self):
        state=self.prefs.read()
        self.assertEqual(state['streamers'][0]['channel_id'],'c1')
        self.prefs.mutate('remove',{'channel_id':'c1'})
        self.assertEqual(Preferences(self.root).read()['streamers'],[])
        self.assertEqual(collector.load_config(self.root/'config.json')['channels'],[])
        self.assertEqual(self.store.rows('SELECT * FROM channels')[0]['input'],'@one')

    def test_recording_switches_pause_and_removal_enforce_writes(self):
        self.update(record_chat=False)
        self.store.save_batch(self.broadcast,[self.text,paid('paid1')],None)
        self.assertEqual(len(self.store.chat.rows()),0)
        self.assertEqual(len(self.store.rows('SELECT * FROM superchats')),1)
        self.update(record_paid=False)
        self.store.save_batch(self.broadcast,[self.text,paid('paid2')],None)
        self.assertEqual(len(self.store.chat.rows()),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM superchats')),1)
        self.prefs.mutate('pause',{'paused':True})
        self.store.save_batch(self.broadcast,[{**self.text,'id':'paused'},paid('paid3')],None)
        self.prefs.mutate('remove',{'channel_id':'c1'})
        self.store.save_batch(self.broadcast,[{**self.text,'id':'removed'},paid('paid4')],None)
        self.assertEqual(len(self.store.chat.rows()),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM superchats')),1)

    def test_add_deduplicates_channel_and_options_validate_atomically(self):
        self.prefs.mutate('add',{'name':'two'},lambda name:'c2')
        with self.assertRaises(ValueError): self.prefs.mutate('add',{'name':'@alias'},lambda name:'c2')
        with self.assertRaises(ValueError): self.update(record_chat=False,record_paid=False)
        options={**self.prefs.read()['options'],'public_check_interval_seconds':30,'dashboard_refresh_seconds':0}
        with self.assertRaises(ValueError): self.prefs.mutate('options',options)
        self.assertEqual(self.prefs.read()['options']['public_check_interval_seconds'],60)
        options.update(dashboard_refresh_seconds=5)
        self.prefs.mutate('options',options)
        self.assertEqual(Preferences(self.root).read()['options']['dashboard_refresh_seconds'],5)

    def test_retention_separates_message_types_and_keeps_metadata(self):
        self.store.save_batch(self.broadcast,[self.text,paid('paid1')],None)
        self.store.upsert_video('v1','c1','Stream')
        old=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
        with closing(self.store.chat.connect()) as db,db:
            db.execute('UPDATE chat_messages SET received_at=?',(old,))
        self.store.execute('UPDATE superchats SET received_at=?',(old,))
        self.prefs.mutate('options',{**self.prefs.read()['options'],'chat_retention_days':7})
        self.prefs.retain()
        self.assertEqual(self.store.chat.rows(),[])
        self.assertEqual(len(self.store.rows('SELECT * FROM superchats')),1)
        self.prefs.mutate('options',{**self.prefs.read()['options'],'paid_retention_days':7})
        self.prefs.retain()
        self.assertEqual(self.store.rows('SELECT * FROM superchats'),[])
        self.assertEqual(len(self.store.rows('SELECT * FROM broadcasts')),1)

    def test_export_filters_and_csv_escaping(self):
        self.store.save_batch(self.broadcast,[self.text,paid('paid1')],None)
        query={'kind':['chat'],'channel':['c1'],'video':['v1'],
               'from':['2026-11-18T00:00:00Z'],'to':['2026-11-19T00:00:00Z']}
        path,fields,sql,params=export_query(self.root,query)
        rows=list(csv.DictReader(io.StringIO(b''.join(csv_lines(path,fields,sql,params)).decode('utf-8-sig'))))
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['message'],"'=formula")
        query['channel']=['other']; args=export_query(self.root,query)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(b''.join(csv_lines(*args)).decode('utf-8-sig'))))),0)
        with self.assertRaises(ValueError): export_query(self.root,{'kind':['bad']})

    def test_post_rejects_cross_origin_and_accepts_valid_local_settings(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),dashboard.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch.object(dashboard,'ROOT',self.root):
                url='http://127.0.0.1:'+str(server.server_port)+'/api/settings'
                headers={'Host':'127.0.0.1:8765','Content-Type':'application/json',
                         'X-Settings-Token':dashboard.SETTINGS_TOKEN,'Origin':'https://untrusted.example'}
                payload=json.dumps({'action':'pause','paused':True}).encode()
                with self.assertRaises(HTTPError) as rejected: urlopen(Request(url,payload,headers))
                self.assertEqual(rejected.exception.code,403)
                self.assertFalse(self.prefs.read()['options']['paused'])
                headers['Origin']=dashboard.URL
                with urlopen(Request(url,payload,headers)) as response:
                    self.assertTrue(json.load(response)['saved'])
                self.assertTrue(self.prefs.read()['options']['paused'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__=='__main__': unittest.main()
