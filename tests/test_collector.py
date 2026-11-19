import concurrent.futures
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector


def paid(message_id='m1', sticker=False):
    details = {'amountMicros': '5000000', 'currency': 'USD', 'amountDisplayString': '$5.00'}
    if not sticker:
        details['userComment'] = 'A test message'
    return {'id': message_id, 'snippet': {
        'type': 'superStickerEvent' if sticker else 'superChatEvent',
        'publishedAt': '2026-11-18T12:00:00Z',
        'superStickerDetails' if sticker else 'superChatDetails': details},
        'authorDetails': {'channelId': 'donor1', 'displayName': 'Donor'}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = collector.Store(Path(self.temp.name) / 'test.sqlite3')
        self.broadcast = {'video_id': 'v1', 'channel_id': 'c1', 'chat_id': 'chat1', 'title': 'Test'}

    def tearDown(self):
        self.temp.cleanup()

    def test_dedup_stickers_and_restart_cursor(self):
        self.store.save_batch(self.broadcast, [paid(), paid(), paid('s1', True)], 'token1')
        self.store.save_batch(self.broadcast, [paid()], 'token2')
        restarted = collector.Store(self.store.path)
        self.assertEqual(len(restarted.rows('SELECT * FROM superchats')), 2)
        self.assertEqual(restarted.token('chat1'), 'token2')
        self.assertEqual(restarted.rows('SELECT amount_micros FROM superchats')[0]['amount_micros'], 5000000)

    def test_malformed_batch_rolls_back_events_and_cursor(self):
        self.store.save_batch(self.broadcast, [], 'before')
        bad = paid('bad'); bad['snippet']['superChatDetails']['amountMicros'] = 'invalid'
        with self.assertRaises(ValueError):
            self.store.save_batch(self.broadcast, [paid(), bad], 'after')
        self.assertEqual(self.store.rows('SELECT * FROM superchats'), [])
        self.assertEqual(self.store.token('chat1'), 'before')

    def test_unpaid_messages_still_advance_cursor(self):
        self.store.save_batch(self.broadcast, [{'id': 'text', 'snippet': {'type': 'textMessageEvent'}}], 'next')
        self.assertEqual(self.store.token('chat1'), 'next')
        self.assertEqual(self.store.rows('SELECT * FROM superchats'), [])

    def test_upsert_does_not_reset_ended_state(self):
        self.store.upsert_video('v1', 'c1', 'title')
        self.store.execute("UPDATE broadcasts SET status='ended' WHERE video_id='v1'")
        self.store.upsert_video('v1', 'c1', 'updated title')
        self.assertEqual(self.store.rows('SELECT status FROM broadcasts')[0]['status'], 'ended')


class DiscoveryTests(unittest.TestCase):
    def test_live_upcoming_and_pagination(self):
        api = collector.YouTube('fake')
        requests = []
        def get(resource, **params):
            requests.append(params)
            if params['eventType'] == 'live' and not params.get('pageToken'):
                return {'items': [{'id': {'videoId': 'a'}}], 'nextPageToken': 'page2'}
            return {'items': [{'id': {'videoId': params['eventType']}}]}
        with patch.object(api, 'get', side_effect=get):
            self.assertEqual(len(api.discover('c1')), 3)
        self.assertEqual(requests[1]['pageToken'], 'page2')
        self.assertEqual(requests[2]['eventType'], 'upcoming')

    def test_handle_and_url_resolution(self):
        api = collector.YouTube('fake')
        with patch.object(api, 'get', return_value={'items': [{'id': 'UC123'}]}) as get:
            self.assertEqual(api.resolve('https://www.youtube.com/@Hutch'), 'UC123')
            self.assertEqual(get.call_args.kwargs['forHandle'], '@Hutch')


class StreamingTests(unittest.TestCase):
    setUp = StorageTests.setUp
    tearDown = StorageTests.tearDown

    def test_grpc_disconnect_resume_and_duplicate_delivery(self):
        import grpc
        from google.protobuf.json_format import ParseDict
        import stream_list_pb2 as pb
        import stream_list_pb2_grpc as rpc
        collector.STOP.clear()
        store, broadcast = self.store, self.broadcast
        store.upsert_video('v1', 'c1', 'Test')
        store.execute("UPDATE broadcasts SET chat_id='chat1',status='live' WHERE video_id='v1'")
        tokens = []

        class Service(rpc.V3DataLiveChatMessageServiceServicer):
            def StreamList(self, request, context):
                tokens.append(request.page_token)
                event = paid()
                event['snippet']['type'] = 'SUPER_CHAT_EVENT'
                batch = pb.LiveChatMessageListResponse(next_page_token='resume1')
                ParseDict(event, batch.items.add())
                yield batch
                if len(tokens) == 1:
                    context.abort(grpc.StatusCode.UNAVAILABLE, 'simulated interruption')
                ended = pb.LiveChatMessageListResponse(next_page_token='resume2')
                ParseDict(event, ended.items.add())
                ended.items.add().snippet.type = pb.LiveChatMessageSnippet.TypeWrapper.CHAT_ENDED_EVENT
                yield ended

        server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        rpc.add_V3DataLiveChatMessageServiceServicer_to_server(Service(), server)
        port = server.add_insecure_port('127.0.0.1:0')
        server.start()
        client = grpc.insecure_channel(f'127.0.0.1:{port}')
        try:
            with patch('grpc.secure_channel', return_value=client):
                collector.listen(store, collector.YouTube('test-key'), broadcast)
            self.assertEqual(tokens, ['', 'resume1'])
            self.assertEqual(store.token('chat1'), 'resume2')
            rows = store.rows('SELECT * FROM superchats')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['donor_channel_id'], 'donor1')
            self.assertEqual(rows[0]['event_type'], 'superChatEvent')
            self.assertEqual(store.rows('SELECT status FROM broadcasts')[0]['status'], 'ended')
        finally:
            client.close()
            server.stop(0).wait()


class NormalChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = collector.Store(root / 'paid.sqlite3', root / 'normal.sqlite3')
        self.broadcast = {'video_id': 'v1', 'channel_id': 'c1', 'chat_id': 'chat1', 'title': 'Test'}
        self.normal = {'id': 'text1', 'snippet': {'type': 'textMessageEvent',
            'publishedAt': '2026-11-18T12:00:00Z', 'textMessageDetails': {'messageText': 'Hello chat!'}},
            'authorDetails': {'channelId': 'author1', 'displayName': 'Viewer'}}

    def tearDown(self):
        self.temp.cleanup()

    def test_separate_files_and_deduplication(self):
        for _ in range(2):
            self.store.save_batch(self.broadcast, [self.normal, paid()], 'next')
        self.assertEqual(len(self.store.rows('SELECT * FROM superchats')), 1)
        self.assertEqual(len(self.store.chat.rows()), 1)
        row = self.store.chat.rows()[0]
        self.assertEqual(row['message'], 'Hello chat!')
        self.assertEqual(row['author_channel_id'], 'author1')
        self.assertEqual(self.store.rows("SELECT name FROM sqlite_master WHERE name='chat_messages'"), [])

    def test_chat_failure_does_not_advance_cursor(self):
        with patch.object(self.store.chat, 'save_batch', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.store.save_batch(self.broadcast, [self.normal, paid()], 'next')
        self.assertIsNone(self.store.token('chat1'))
        self.assertEqual(self.store.rows('SELECT * FROM superchats'), [])

    def test_old_raw_metadata_column_removed_without_changing_parsed_messages(self):
        self.store.save_batch(self.broadcast, [self.normal, paid()], None)
        before = self.store.chat.rows()
        from contextlib import closing
        with closing(self.store.chat.connect()) as db, db:
            db.execute("ALTER TABLE chat_messages ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{}'")
        migrated = collector.ChatStore(self.store.chat.path)
        self.assertEqual(migrated.rows(), before)
        self.assertNotIn('raw_json', migrated.rows()[0])
        self.assertIn('raw_json', self.store.rows('SELECT * FROM superchats')[0])
        # Inserts still work after migration and duplicate IDs remain unique.
        migrated.save_batch(self.broadcast, [self.normal])
        self.assertEqual(len(migrated.rows()), 1)

    def test_paid_failure_then_replay_does_not_duplicate_normal_chat(self):
        broken = paid(); broken['snippet']['superChatDetails']['amountMicros'] = 'invalid'
        with self.assertRaises(ValueError):
            self.store.save_batch(self.broadcast, [self.normal, broken], 'next')
        self.assertEqual(len(self.store.chat.rows()), 1)
        self.assertIsNone(self.store.token('chat1'))
        self.store.save_batch(self.broadcast, [self.normal, paid()], 'next')
        self.assertEqual(len(self.store.chat.rows()), 1)
        self.assertEqual(self.store.token('chat1'), 'next')

    def test_website_normal_chat_is_saved(self):
        action = {'liveChatTextMessageRenderer': {'id': 'webtext',
            'authorName': {'simpleText': '@Viewer'}, 'authorExternalChannelId': 'author1',
            'message': {'runs': [{'text': 'Hello '}, {'emoji': {'shortcuts': [':wave:']}}]}}}
        events = collector.website_events(action)
        self.store.save_batch(self.broadcast, events, None)
        self.assertEqual(self.store.chat.rows()[0]['message'], 'Hello :wave:')
        self.assertEqual(self.store.rows('SELECT * FROM superchats'), [])

    def test_same_database_path_rejected(self):
        with self.assertRaises(ValueError):
            collector.Store(self.store.path, self.store.path)


class PublicDiscoveryTests(unittest.TestCase):
    setUp = StorageTests.setUp

    def tearDown(self):
        collector.STOP.clear()
        collector.WAKE.clear()
        self.temp.cleanup()

    def extract(self, info=None, error=None):
        patcher = patch('yt_dlp.YoutubeDL')
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        extract = mock.return_value.__enter__.return_value.extract_info
        extract.return_value = info
        extract.side_effect = error
        return extract

    def test_live_and_upcoming_keep_video_ids(self):
        extract = self.extract({'channel_id': 'c1', 'id': 'v1', 'live_status': 'is_live', 'title': 'Test'})
        self.assertEqual(collector.check_live_page('c1')['video_id'], 'v1')
        extract.return_value['live_status'] = 'is_upcoming'
        self.assertEqual(collector.check_live_page('c1')['status'], 'is_upcoming')

    def test_offline_is_separate_from_network_failure(self):
        from yt_dlp.utils import DownloadError, UserNotLive
        cause = UserNotLive(video_id='c1')
        extract = self.extract(error=DownloadError('offline', exc_info=(UserNotLive, cause, None)))
        self.assertEqual(collector.check_live_page('c1')['status'], 'offline')
        extract.side_effect = DownloadError('network failure')
        with self.assertRaises(collector.LivePageError):
            collector.check_live_page('c1')

    def test_live_metrics_preserve_zero_viewers_and_start_time(self):
        extract = self.extract({'channel_id':'c1','id':'v1','live_status':'is_live',
            'concurrent_view_count':0,'view_count':624,'release_timestamp':1795000000})
        result = collector.check_live_page('c1')
        self.assertEqual(result['concurrent_view_count'],0)
        self.assertEqual(result['view_count'],624)
        self.assertEqual(result['live_started_at'],1795000000)
        extract.return_value['live_status']='is_upcoming'
        self.assertNotIn('live_started_at',collector.check_live_page('c1'))

    def test_mismatched_channel_is_rejected(self):
        self.extract({'channel_id': 'another-channel', 'id': 'v1', 'live_status': 'is_live'})
        with self.assertRaises(collector.LivePageError):
            collector.check_live_page('c1')

    def test_public_detection_saves_candidate_and_wakes_status_check(self):
        collector.STOP.clear()
        with patch.object(collector.STOP, 'wait', side_effect=lambda delay: collector.STOP.set()):
            collector.watch_live_page(self.store, 'c1', 60, check=lambda channel: {
                'status': 'is_live', 'video_id': 'new-live', 'title': 'New stream'})
        self.assertTrue(collector.WAKE.is_set())
        rows = self.store.rows('SELECT * FROM broadcasts')
        self.assertEqual(rows[0]['video_id'], 'new-live')
        self.assertEqual(rows[0]['status'], 'live')

    def test_errors_back_off_without_marking_broadcast_offline(self):
        collector.STOP.clear()
        self.store.upsert_video('existing', 'c1', 'Existing')
        delays = []
        def wait(delay):
            delays.append(delay)
            if len(delays) == 3:
                collector.STOP.set()
        def broken(channel):
            raise collector.LivePageError('temporary failure')
        with patch.object(collector.STOP, 'wait', side_effect=wait):
            collector.watch_live_page(self.store, 'c1', 60, check=broken)
        self.assertEqual(delays, [60, 120, 240])
        self.assertEqual(self.store.rows('SELECT status FROM broadcasts')[0]['status'], 'pending')
        self.assertIn('"status": "error"', self.store.get_state('public-check:c1'))


class WebsiteStorageTests(unittest.TestCase):
    setUp = StorageTests.setUp
    tearDown = StorageTests.tearDown

    def renderer(self, message_id='web1'):
        return {'id': message_id, 'authorName': {'simpleText': '@Donor'},
                'authorExternalChannelId': 'donor1', 'timestampUsec': '1795003200000000',
                'purchaseAmountText': {'simpleText': '$5.00'},
                'message': {'runs': [{'text': 'Hello from the live chat'}]}}

    def test_website_paid_message_and_sticker_keep_display_without_guessing_currency(self):
        action = {'actions': [{'addChatItemAction': {'item': {'liveChatPaidMessageRenderer': self.renderer()}}},
                              {'addChatItemAction': {'item': {'liveChatPaidStickerRenderer': self.renderer('sticker')}}}]}
        events = collector.website_events(action)
        self.store.save_batch(self.broadcast, events, None)
        self.store.save_batch(self.broadcast, events, None)
        rows = self.store.rows('SELECT * FROM superchats')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['amount_display'], '$5.00')
        self.assertIsNone(rows[0]['currency'])
        self.assertIsNone(rows[0]['amount_micros'])
        self.assertEqual(json.loads(rows[0]['raw_json'])['_source'], 'yt-dlp')

    def test_partial_line_waits_until_complete(self):
        data = json.dumps({'liveChatPaidMessageRenderer': self.renderer()}).encode()
        stream = io.BytesIO(data)
        self.assertEqual(collector.ingest_chat_lines(self.store, self.broadcast, stream), 0)
        self.assertEqual(stream.tell(), 0)
        stream.seek(0, 2); stream.write(b'\n'); stream.seek(0)
        self.assertEqual(collector.ingest_chat_lines(self.store, self.broadcast, stream), 1)
        self.assertEqual(collector.ingest_chat_lines(self.store, self.broadcast, stream), 0)

    def test_existing_database_migrates_without_losing_events(self):
        from contextlib import closing
        import sqlite3
        legacy_path = Path(self.temp.name) / 'legacy.sqlite3'
        schema = self.store.rows("SELECT sql FROM sqlite_master WHERE name='superchats'")[0]['sql']
        schema = schema.replace('amount_micros INTEGER, currency TEXT,',
                                'amount_micros INTEGER NOT NULL, currency TEXT NOT NULL,')
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute(schema)
        legacy = collector.Store(legacy_path)
        legacy.save_batch(self.broadcast, [paid()], None)
        legacy.save_batch(self.broadcast, collector.website_events({'liveChatPaidMessageRenderer': self.renderer()}), None)
        self.assertEqual(len(legacy.rows('SELECT * FROM superchats')), 2)

    def test_default_mode_does_not_require_api_key(self):
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps({'channels': ['@Hutch']}))
        with patch.dict('os.environ', {}, clear=True), patch('collector.run_ytdlp') as run:
            collector.run(path)
        self.assertEqual(run.call_args.args[1]['mode'], 'ytdlp')

    def test_downloader_output_is_ingested_before_shutdown(self):
        from unittest.mock import Mock
        collector.STOP.clear()
        process = Mock()
        process.poll.return_value = None
        def launch(args, **kwargs):
            template = args[args.index('-o') + 1]
            output = Path(template.replace('.%(ext)s', '.live_chat.json'))
            output.write_text(json.dumps({'liveChatPaidMessageRenderer': self.renderer()}) + '\n', encoding='utf-8')
            self.assertIn('--no-part', args)
            process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
            return process
        try:
            with patch('collector.subprocess.Popen', side_effect=launch), patch.object(
                    collector.STOP, 'wait', side_effect=lambda delay: collector.STOP.set() or True):
                collector.listen_ytdlp(self.store, self.broadcast)
            rows = self.store.rows('SELECT * FROM superchats')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['amount_display'], '$5.00')
            process.terminate.assert_called_once()
        finally:
            collector.STOP.clear()


if __name__ == '__main__':
    unittest.main()
