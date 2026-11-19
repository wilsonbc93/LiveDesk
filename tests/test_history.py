from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
import collector
import dashboard
from tests.test_collector import paid


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'config.json').write_text(json.dumps({'channels':['@one','@two']}))
        self.store=collector.Store(self.root/'superchats.sqlite3',self.root/'chat-messages.sqlite3')
        for name,cid in [('@one','c1'),('@two','c2')]:
            self.store.execute('INSERT INTO channels VALUES (?,?)',(name,cid))
        for vid,cid,title in [('old','c1','A past discussion'),('new','c1','Current stream'),('other','c2','Another broadcast')]:
            self.store.upsert_video(vid,cid,title)
            messages=[{'id':vid+str(i),'snippet':{'type':'textMessageEvent',
                'publishedAt':'2026-11-18T12:00:00Z','textMessageDetails':{'messageText':'hello '+str(i)}},
                'authorDetails':{'displayName':'Viewer'}} for i in range(7)]
            self.store.save_batch({'video_id':vid,'channel_id':cid,'chat_id':vid},messages+[paid('paid-'+vid)],None)
        self.store.record_public_result('c1',{'status':'is_live','video_id':'new','title':'Current stream',
            'live_started_at':datetime(2026,11,18,13,tzinfo=timezone.utc).timestamp()})
        self.store.record_public_result('c2',{'status':'offline'})

    def tearDown(self): self.temp.cleanup()

    def test_catalog_excludes_current_stream_and_finds_saved_history(self):
        data=dashboard.history_list(self.root,{})
        self.assertEqual({r['video_id'] for r in data['streams']},{'old','other'})
        self.assertEqual(data['total'],2)
        self.assertEqual(data['streams'][0]['chat_count'],7)
        self.assertEqual(data['streams'][0]['donations']['total_cents'],500)
        self.assertIsNotNone(data['streams'][0]['date_at'])

    def test_title_channel_and_date_filters(self):
        data=dashboard.history_list(self.root,{'channel':['c1'],'q':['past'],
            'from':['2026-11-18T00:00:00Z'],'to':['2026-11-19T00:00:00Z']})
        self.assertEqual([r['video_id'] for r in data['streams']],['old'])
        self.assertEqual(dashboard.history_list(self.root,{'from':['2026-11-19T00:00:00Z']})['total'],0)

    def test_message_pagination_and_search(self):
        first=dashboard.history_messages(self.root,{'video':['old'],'limit':['3']})
        second=dashboard.history_messages(self.root,{'video':['old'],'limit':['3'],'offset':['3']})
        self.assertEqual(first['total'],7)
        self.assertEqual(len(first['messages']),3)
        self.assertFalse({r['message_id'] for r in first['messages']} & {r['message_id'] for r in second['messages']})
        search=dashboard.history_messages(self.root,{'video':['old'],'q':['hello 5']})
        self.assertEqual(search['total'],1)
        paid_rows=dashboard.history_messages(self.root,{'video':['old'],'kind':['superchats']})
        self.assertEqual(paid_rows['total'],1)
        self.assertNotIn('raw_json',paid_rows['messages'][0])

    def test_offline_transition_preserves_metadata_and_adds_stream_to_history(self):
        self.store.record_public_result('c1',{'status':'offline'})
        detail=dashboard.history_detail(self.root,{'video':['new']})
        self.assertEqual(detail['title'],'Current stream')
        self.assertIsNotNone(detail['started_at'])
        self.assertIsNotNone(detail['observed_offline_at'])
        self.assertEqual(detail['donations']['total_cents'],500)

    def test_username_and_message_filters_are_separate_and_combine(self):
        self.store.execute('UPDATE superchats SET donor_name=? WHERE video_id=?',('@Alice','old'))
        with closing(self.store.chat.connect()) as db, db:
            db.execute('UPDATE chat_messages SET author_name=?,message=? WHERE message_id=?',
                       ('@Alice','hello from Alice','old0'))
            db.execute('UPDATE chat_messages SET message=? WHERE message_id=?',('@Alice hello','old1'))
        query={'video':['old'],'username':['@aLiCe']}
        result=dashboard.history_messages(self.root,query)
        self.assertEqual([m['message_id'] for m in result['messages']],['old0'])
        self.assertEqual(dashboard.history_messages(self.root,{**query,'q':['from']})['total'],1)
        self.assertEqual(dashboard.history_messages(self.root,{**query,'q':['missing']})['total'],0)
        self.assertEqual(dashboard.history_messages(self.root,{'video':['old'],'q':['Viewer']})['total'],0)
        self.assertEqual(dashboard.history_messages(self.root,{**query,'kind':['superchats']})['total'],1)

    def test_rejects_live_stream_and_invalid_pagination(self):
        with self.assertRaises(ValueError): dashboard.history_detail(self.root,{'video':['new']})
        with self.assertRaises(ValueError): dashboard.history_messages(self.root,{'video':['old'],'offset':['-1']})
        with self.assertRaises(ValueError): dashboard.history_messages(self.root,{'video':['old'],'kind':['invalid']})

    def test_failed_live_check_does_not_archive_active_stream(self):
        self.store.record_public_result('c1',{'status':'error'})
        data=dashboard.history_list(self.root,{})
        self.assertNotIn('new',{r['video_id'] for r in data['streams']})


if __name__=='__main__': unittest.main()
