from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
import collector
import user_search


class UserSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'config.json').write_text(json.dumps({'channels':[]}))
        self.store=collector.Store(self.root/'superchats.sqlite3',self.root/'chat-messages.sqlite3')
        for cid,name,vid in [('s1','@one','v1'),('s2','@two','v2'),('s3','@removed','v3')]:
            self.store.execute('INSERT INTO channels VALUES (?,?)',(name,cid))
            self.store.upsert_video(vid,cid,'Title '+vid)
        self.add('a1','v1','s1','A','@Before','2026-11-18T10:00:00Z','Hello @OnlyMention')
        self.add('a2','v2','s2','A','@After','2026-11-19T10:00:00Z','Other conversation')
        self.add('b1','v1','s1','B','@After','2026-11-19T11:00:00Z','Different person')
        self.add('n1','v3','s3',None,'@After','2026-11-19T12:00:00Z','Unknown identity')
        self.add('p1','v2','s2','A','@After','2026-11-19T13:00:00Z','Paid message','$5.00')
        self.add('p2','v3','s3','A','@After','2026-11-19T14:00:00Z','Canadian message','CA$7.00')
        self.store.record_public_result('s1',{'status':'is_live','video_id':'v1','title':'Title v1'})

    def tearDown(self): self.temp.cleanup()

    def add(self,mid,vid,streamer,author,name,at,message,amount=None):
        event={'id':mid,'snippet':{'type':'superChatEvent' if amount else 'textMessageEvent','publishedAt':at},
               'authorDetails':{'channelId':author,'displayName':name}}
        event['snippet']['superChatDetails' if amount else 'textMessageDetails']=(
            {'amountDisplayString':amount,'userComment':message} if amount else {'messageText':message})
        self.store.save_batch({'video_id':vid,'channel_id':streamer,'chat_id':vid},[event],None)

    def test_same_names_do_not_merge_distinct_ids_or_unverified_matches(self):
        data=user_search.users(self.root,{'q':['after']})
        self.assertEqual({u['user_key'] for u in data['users']},{'id:A','id:B','name:@After'})
        self.assertEqual(next(u for u in data['users'] if u['user_key']=='id:A')['message_count'],4)
        self.assertFalse(next(u for u in data['users'] if u['user_key']=='name:@After')['verified'])
        unknown=user_search.profile(self.root,{'user':['name:@After']})
        self.assertEqual(unknown['message_count'],1)
        self.assertFalse(unknown['verified'])

    def test_old_names_find_complete_identity_and_ignore_mentions(self):
        result=user_search.users(self.root,{'q':['@BEFORE']})['users']
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['name'],'@After')
        self.assertEqual(result[0]['message_count'],4)
        self.assertEqual(user_search.users(self.root,{'q':['OnlyMention']})['total'],0)

    def test_profile_includes_live_and_past_streams_and_usd_only(self):
        p=user_search.profile(self.root,{'user':['id:A']})
        self.assertEqual((p['chat_count'],p['paid_count'],p['stream_count'],p['streamer_count']),(2,2,3,3))
        self.assertEqual(p['total_cents'],500)
        self.assertEqual(set(p['aliases']),{'@Before','@After'})
        self.assertEqual({s['video_id'] for s in p['streams']},{'v1','v2','v3'})
        self.assertEqual({s['name'] for s in p['streamers']},{'@one','@two','@removed'})

    def test_activity_filters_combine_and_paginate_without_raw_metadata(self):
        q={'user':['id:A']}
        first=user_search.messages(self.root,{**q,'limit':['2']})
        second=user_search.messages(self.root,{**q,'limit':['2'],'offset':['2']})
        self.assertEqual(first['total'],4)
        self.assertFalse({r['message_id'] for r in first['messages']} & {r['message_id'] for r in second['messages']})
        result=user_search.messages(self.root,{**q,'channel':['s2'],'video':['v2'],'kind':['superchats'],
            'q':['paid'],'from':['2026-11-19T00:00:00Z'],'to':['2026-11-20T00:00:00Z']})
        self.assertEqual([r['message_id'] for r in result['messages']],['p1'])
        self.assertEqual(result['messages'][0]['title'],'Title v2')
        self.assertEqual(result['messages'][0]['streamer'],'@two')
        self.assertNotIn('raw_json',result['messages'][0])
        self.assertEqual(user_search.messages(self.root,{**q,'q':['%']})['total'],0)

    def test_no_matches_and_invalid_inputs(self):
        self.assertEqual(user_search.users(self.root,{'q':['x']})['total'],0)
        with self.assertRaises(ValueError): user_search.profile(self.root,{'user':['id:missing']})
        for query in [{'user':['bad']},{'user':['id:A'],'kind':['wrong']},{'user':['id:A'],'offset':['-1']},
                      {'user':['id:A'],'from':['2026-11-19']}]:
            with self.assertRaises(ValueError): user_search.messages(self.root,query)

    def test_missing_databases_are_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'config.json').write_text('{}')
            self.assertEqual(user_search.users(root,{'q':['viewer']})['total'],0)
            self.assertEqual({p.name for p in root.iterdir()},{'config.json'})


if __name__=='__main__': unittest.main()
