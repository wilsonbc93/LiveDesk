from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector
import dashboard
from tests.test_collector import paid


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'channels': ['@one', '@two']}))
        self.store = collector.Store(self.root / 'superchats.sqlite3', self.root / 'chat-messages.sqlite3')
        for name, cid, vid in [('@one','c1','v1'),('@two','c2','v2')]:
            self.store.execute('INSERT INTO channels VALUES (?,?)', (name,cid))
            self.store.set_state('public-check:'+cid, json.dumps({'status':'is_live','video_id':vid,
                'title':name+' stream','checked_at':datetime.now(timezone.utc).isoformat()}))
            normal={'id':'text'+cid, 'snippet':{'type':'textMessageEvent',
                'textMessageDetails':{'messageText':'Hello '+cid}},'authorDetails':{'displayName':'Viewer'}}
            self.store.save_batch({'video_id':vid,'channel_id':cid,'chat_id':'chat'+cid},
                                  [normal,paid('paid'+cid)],None)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_and_channel_filter(self):
        with patch('control.running', return_value=True):
            data=dashboard.snapshot(self.root)
            self.assertEqual(len(data['chat']),2)
            self.assertEqual(data['counts'],{'chat':2,'paid':2})
            selected=dashboard.snapshot(self.root,'c1')
        self.assertEqual([r['video_id'] for r in selected['chat']],['v1'])
        self.assertEqual([r['streamer_channel_id'] for r in selected['superchats']],['c1'])
        self.assertNotIn('raw_json',selected['superchats'][0])

    def test_stale_status_excludes_old_stream_chat_but_retains_paid_history(self):
        stale=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.store.set_state('public-check:c1',json.dumps({'status':'is_live','video_id':'v1','checked_at':stale}))
        with patch('control.running', return_value=False):
            data=dashboard.snapshot(self.root,'c1')
        self.assertEqual(data['channels'][0]['status'],'unknown')
        self.assertEqual(data['chat'],[])
        self.assertEqual(len(data['superchats']),1)
        self.assertFalse(data['collector_running'])

    def test_invalid_channel_rejected(self):
        with self.assertRaises(ValueError):
            dashboard.snapshot(self.root,'not-watched')

    def test_live_metrics_and_missing_values(self):
        started=datetime.now(timezone.utc)-timedelta(hours=2)
        self.store.set_state('public-check:c1',json.dumps({'status':'is_live','video_id':'v1',
            'checked_at':datetime.now(timezone.utc).isoformat(),
            'concurrent_view_count':0,'view_count':624,'live_started_at':started.timestamp()}))
        with patch('control.running',return_value=True):
            data=dashboard.snapshot(self.root)
        first,second=data['channels']
        self.assertEqual(first['concurrent_view_count'],0)
        self.assertEqual(first['view_count'],624)
        self.assertGreaterEqual(first['live_elapsed_seconds'],7200)
        self.assertIsNone(second['concurrent_view_count'])
        self.assertIsNone(second['live_elapsed_seconds'])

    def test_upcoming_stream_does_not_show_live_duration(self):
        self.store.set_state('public-check:c1',json.dumps({'status':'is_upcoming','video_id':'v1',
            'checked_at':datetime.now(timezone.utc).isoformat(),'concurrent_view_count':20,
            'live_started_at':1795000000}))
        with patch('control.running',return_value=True):
            first=dashboard.snapshot(self.root)['channels'][0]
        self.assertIsNone(first['concurrent_view_count'])
        self.assertIsNone(first['live_elapsed_seconds'])

    def test_missing_database_is_not_created(self):
        path=self.root/'missing.sqlite3'
        self.assertEqual(dashboard.read_rows(path,'SELECT 1'),[])
        self.assertFalse(path.exists())

    def test_usd_amount_parsing_excludes_foreign_currencies(self):
        for amount,expected in [('$9.99',999),('$1,234.50',123450),('US$5.00',500),
                                ('USD 5.00',500),('5.00 USD',500),('CA$5.00',None),
                                ('A$5.00',None),('NZ$5.00',None),('EUR 5.00',None),
                                ('$1,23',None),('$-5.00',None)]:
            with self.subTest(amount=amount):
                self.assertEqual(dashboard.usd_cents({'amount_display':amount}),expected)
        self.assertIsNone(dashboard.usd_cents({'currency':'CAD','amount_display':'$5.00'}))
        self.assertEqual(dashboard.usd_cents({'currency':'USD','amount_micros':1990000}),199)

    def test_donations_are_per_stream_and_group_donors_by_channel_id(self):
        def add(message_id, donor, name, amount, currency=None):
            event=paid(message_id)
            event['authorDetails']={'channelId':donor,'displayName':name}
            event['snippet']['superChatDetails']={'amountDisplayString':amount,'currency':currency}
            self.store.save_batch({'video_id':'v1','channel_id':'c1','chat_id':'chat1'},[event],None)
        add('extra1','donor1','Changed name','$2.00')
        add('extra2','donor2','Second','$10.00')
        add('extra3','donor3','Third','$3.00')
        add('extra4','donor4','Fourth','$1.00')
        add('foreign','donor2','Second','CA$100.00')
        summaries=dashboard.donation_summaries(self.root/'superchats.sqlite3',{'v1','v2'})
        self.assertEqual(summaries['v1']['total_cents'],2100)  # Initial $5 plus $2+$10+$3+$1.
        self.assertEqual(summaries['v2']['total_cents'],500)
        self.assertEqual([d['total_cents'] for d in summaries['v1']['top_donors']],[1000,700,300])
        self.assertEqual(summaries['v1']['top_donors'][1]['channel_id'],'donor1')
        self.assertEqual(summaries['v1']['payment_count'],5)

    def test_total_is_not_limited_to_recent_sixty_payments(self):
        events=[paid('many'+str(i)) for i in range(65)]
        self.store.save_batch({'video_id':'v1','channel_id':'c1','chat_id':'chat1'},events,None)
        with patch('control.running',return_value=True):
            data=dashboard.snapshot(self.root)
        self.assertEqual(len(data['superchats']),60)
        self.assertEqual(data['channels'][0]['donations']['total_cents'],66*500)
        self.assertEqual(data['channels'][0]['donations']['top_donors'][0]['total_cents'],66*500)


if __name__=='__main__':
    unittest.main()
