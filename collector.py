"""Continuously discover YouTube broadcasts and store paid live chat events."""
import argparse
import concurrent.futures
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

LOG = logging.getLogger('collector')
STOP = threading.Event()
CALLS = set()
CALLS_LOCK = threading.Lock()
WAKE = threading.Event()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path, chat_path=None):
        if chat_path is not None and Path(path).resolve() == Path(chat_path).resolve():
            raise ValueError('Chat and Super Chat databases must use different paths')
        self.path = str(path)
        self.chat = ChatStore(chat_path) if chat_path is not None else None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS channels (
                    input TEXT PRIMARY KEY, channel_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS broadcasts (
                    video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                    title TEXT, chat_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    chat_id TEXT PRIMARY KEY, token TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS superchats (
                    message_id TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                    streamer_channel_id TEXT NOT NULL, live_chat_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, donor_channel_id TEXT,
                    donor_name TEXT, published_at TEXT, received_at TEXT NOT NULL,
                    amount_micros INTEGER, currency TEXT,
                    amount_display TEXT, message TEXT, raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS donor_time ON superchats(donor_channel_id, published_at);
                CREATE INDEX IF NOT EXISTS stream_time ON superchats(video_id, published_at);
            ''')
            existing = {row['name'] for row in db.execute('PRAGMA table_info(broadcasts)')}
            for name, datatype in [('first_seen_at','TEXT'),('live_started_at','REAL'),
                                   ('last_live_at','TEXT'),('ended_at','TEXT')]:
                if name not in existing:
                    db.execute(f'ALTER TABLE broadcasts ADD COLUMN {name} {datatype}')
            db.commit()
            # Website chat exposes a formatted amount, sometimes without an ISO currency.
            # Preserve unknown numeric/currency values as NULL, including in older databases.
            columns = list(db.execute('PRAGMA table_info(superchats)'))
            if any(c['name'] in ('amount_micros', 'currency') and c['notnull'] for c in columns):
                with db:
                    db.execute('ALTER TABLE superchats RENAME TO superchats_old')
                    db.execute('''CREATE TABLE superchats (
                        message_id TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                        streamer_channel_id TEXT NOT NULL, live_chat_id TEXT NOT NULL,
                        event_type TEXT NOT NULL, donor_channel_id TEXT,
                        donor_name TEXT, published_at TEXT, received_at TEXT NOT NULL,
                        amount_micros INTEGER, currency TEXT,
                        amount_display TEXT, message TEXT, raw_json TEXT NOT NULL)''')
                    db.execute('INSERT INTO superchats SELECT * FROM superchats_old')
                    db.execute('DROP TABLE superchats_old')
                    db.execute('CREATE INDEX donor_time ON superchats(donor_channel_id,published_at)')
                    db.execute('CREATE INDEX stream_time ON superchats(video_id,published_at)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def rows(self, sql, params=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def execute(self, sql, params=()):
        with closing(self.connect()) as db, db:
            db.execute(sql, params)

    def get_state(self, key):
        rows = self.rows('SELECT value FROM state WHERE key=?', (key,))
        return rows[0]['value'] if rows else None

    def set_state(self, key, value):
        self.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, str(value)))

    def upsert_video(self, video_id, channel_id, title=''):
        self.execute('''INSERT INTO broadcasts(video_id,channel_id,title,updated_at,first_seen_at) VALUES (?,?,?,?,?)
            ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at''',
            (video_id, channel_id, title, utcnow(), utcnow()))

    def record_public_result(self, channel_id, result):
        now = utcnow()
        self.set_state('public-check:' + channel_id, json.dumps({**result, 'checked_at': now}))
        if result['status'] in ('is_live','is_upcoming'):
            self.upsert_video(result['video_id'],channel_id,result.get('title',''))
            if result['status'] == 'is_live':
                self.execute('''UPDATE broadcasts SET status='live',live_started_at=COALESCE(?,live_started_at),
                    last_live_at=?,ended_at=NULL WHERE video_id=?''',
                    (result.get('live_started_at'),now,result['video_id']))
        elif result['status'] == 'offline':
            # This is the observed offline time, not necessarily the exact broadcast end.
            self.execute("""UPDATE broadcasts SET status='ended',ended_at=COALESCE(ended_at,?),
                updated_at=? WHERE channel_id=? AND status='live'""",(now,now,channel_id))

    def token(self, chat_id):
        rows = self.rows('SELECT token FROM cursors WHERE chat_id=?', (chat_id,))
        return rows[0]['token'] if rows else None

    def save_batch(self, broadcast, items, next_token):
        record_chat,record_paid=True,True
        if (Path(self.path).parent/'settings.sqlite3').exists():
            from preferences import Preferences
            record_chat,record_paid=Preferences(Path(self.path).parent).recording(broadcast['channel_id'])
        # Commit normal chat first: never advance an official cursor past unsaved chat.
        # If paid storage fails afterwards, redelivery is safe because both DBs deduplicate IDs.
        if self.chat is not None and record_chat:
            self.chat.save_batch(broadcast, items)
        inserted = 0
        # Commit the events and cursor together, including batches without paid messages.
        with closing(self.connect()) as db, db:
            for event in items:
                snippet = event.get('snippet', {})
                kind = snippet.get('type')
                if not record_paid or kind not in ('superChatEvent', 'superStickerEvent'):
                    continue
                details = snippet['superChatDetails' if kind == 'superChatEvent' else 'superStickerDetails']
                author = event.get('authorDetails', {})
                result = db.execute('''INSERT OR IGNORE INTO superchats VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                    event['id'], broadcast['video_id'], broadcast['channel_id'], broadcast['chat_id'],
                    kind, author.get('channelId') or snippet.get('authorChannelId'),
                    author.get('displayName'), snippet.get('publishedAt'), utcnow(),
                    int(details['amountMicros']) if details.get('amountMicros') is not None else None,
                    details.get('currency'), details.get('amountDisplayString'),
                    details.get('userComment', ''), json.dumps(event, ensure_ascii=False)))
                inserted += result.rowcount
            if next_token:
                db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?)',
                           (broadcast['chat_id'], next_token, utcnow()))
        return inserted


class ChatStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                    streamer_channel_id TEXT NOT NULL, live_chat_id TEXT NOT NULL,
                    author_channel_id TEXT, author_name TEXT,
                    published_at TEXT, received_at TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_author_time ON chat_messages(author_channel_id,published_at);
                CREATE INDEX IF NOT EXISTS chat_video_time ON chat_messages(video_id,published_at);
            ''')
            if any(row['name'] == 'raw_json' for row in db.execute('PRAGMA table_info(chat_messages)')):
                with db:
                    db.execute('ALTER TABLE chat_messages DROP COLUMN raw_json')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def rows(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM chat_messages ORDER BY published_at,message_id')]

    def save_batch(self, broadcast, items):
        with closing(self.connect()) as db, db:
            for event in items:
                snippet = event.get('snippet', {})
                if snippet.get('type') != 'textMessageEvent':
                    continue
                author = event.get('authorDetails', {})
                db.execute('''INSERT OR IGNORE INTO chat_messages
                    (message_id,video_id,streamer_channel_id,live_chat_id,author_channel_id,
                     author_name,published_at,received_at,message) VALUES (?,?,?,?,?,?,?,?,?)''', (
                    event['id'], broadcast['video_id'], broadcast['channel_id'], broadcast['chat_id'],
                    author.get('channelId') or snippet.get('authorChannelId'), author.get('displayName'),
                    snippet.get('publishedAt'), utcnow(),
                    snippet.get('textMessageDetails', {}).get('messageText', snippet.get('displayMessage', ''))))


class APIError(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(f'YouTube API HTTP {status}: {reason}')


class LivePageError(Exception):
    """A failed check is unknown status, not evidence the channel is offline."""


class QuietDownloaderLog:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def check_live_page(channel_id):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, UserNotLive
    try:
        with YoutubeDL({
            'quiet': True, 'no_warnings': True, 'logger': QuietDownloaderLog(),
            'skip_download': True, 'ignore_no_formats_error': True,
            'cachedir': False, 'socket_timeout': 20, 'retries': 1, 'extractor_retries': 1,
        }) as downloader:
            info = downloader.extract_info(
                f'https://www.youtube.com/channel/{channel_id}/live', download=False)
    except DownloadError as exc:
        cause = exc.exc_info[1] if exc.exc_info else None
        if isinstance(cause, UserNotLive):
            return {'status': 'offline'}
        raise LivePageError('Public live-page check failed; status is unknown') from None
    if not isinstance(info, dict) or info.get('channel_id') != channel_id:
        raise LivePageError('Public live-page result did not identify the expected channel')
    status = info.get('live_status')
    if status in ('is_live', 'is_upcoming') and info.get('id'):
        result = {'status': status, 'video_id': info['id'], 'title': info.get('fulltitle') or info.get('title', '')}
        if status == 'is_live':
            result.update(concurrent_view_count=info.get('concurrent_view_count'),
                          view_count=info.get('view_count'), live_started_at=info.get('release_timestamp'))
        return result
    if status in ('not_live', 'was_live', 'post_live'):
        return {'status': 'offline'}
    raise LivePageError('Public live-page status was unrecognized')


def watch_live_page(store, channel_id, interval, check=None):
    check = check or check_live_page
    failures = 0
    previous = None
    while not STOP.is_set():
        try:
            result = check(channel_id)
            failures = 0
            result['last_success_at']=utcnow()
            store.record_public_result(channel_id, result)
            current = (result['status'], result.get('video_id'))
            if current != previous:
                LOG.info('Public check %s: %s %s', channel_id, *current)
                previous = current
            if result['status'] in ('is_live', 'is_upcoming'):
                if result['status'] == 'is_live':
                    WAKE.set()  # Prompt official API verification instead of waiting for the next tick.
            delay = interval
        except Exception as exc:
            failures += 1
            delay = min(900, interval * 2 ** min(failures - 1, 8))
            previous_state=json.loads(store.get_state('public-check:'+channel_id) or '{}')
            store.set_state('public-check:' + channel_id,
                            json.dumps({'status': 'error', 'checked_at': utcnow(),
                                        'last_success_at':previous_state.get('last_success_at'),
                                        'error':'YouTube status check failed ('+type(exc).__name__+'). Retrying automatically.'}))
            LOG.warning('Public check %s failed (%s); status unknown; retry in %ss',
                        channel_id, type(exc).__name__, delay)
        STOP.wait(delay)


def resolve_public(name):
    from yt_dlp import YoutubeDL
    name = name.strip().rstrip('/')
    if '/channel/' in name:
        name = name.split('/channel/')[-1]
    elif '/@' in name:
        name = '@' + name.split('/@')[-1].split('/')[0]
    if re.fullmatch(r'UC[\w-]{22}', name):
        return name
    if not re.fullmatch(r'@?[\w.-]+', name):
        raise ValueError('Use a channel handle or canonical channel URL')
    handle = name if name.startswith('@') else '@' + name
    with YoutubeDL({'quiet': True, 'logger': QuietDownloaderLog(), 'extract_flat': True,
                    'playlistend': 1, 'skip_download': True, 'cachedir': False,
                    'socket_timeout': 20, 'retries': 1}) as downloader:
        info = downloader.extract_info('https://www.youtube.com/' + handle, download=False)
    channel_id = info.get('channel_id') or info.get('id')
    if not channel_id or not re.fullmatch(r'UC[\w-]{22}', channel_id):
        raise ValueError(f'Could not resolve channel: {name}')
    return channel_id


def renderer_text(value):
    return value.get('simpleText') or ''.join(
        run.get('text') or next(iter(run.get('emoji', {}).get('shortcuts', [])), '')
        for run in value.get('runs', []))


def website_events(action):
    """Normalize text and paid renderers; ticker copies deliberately are not extracted."""
    events = {}
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for kind, item in value.items():
                if kind in ('liveChatPaidMessageRenderer', 'liveChatPaidStickerRenderer', 'liveChatTextMessageRenderer') and item.get('id'):
                    is_chat = kind == 'liveChatPaidMessageRenderer'
                    published = None
                    if item.get('timestampUsec'):
                        published = datetime.fromtimestamp(int(item['timestampUsec']) / 1000000, timezone.utc).isoformat()
                    details = {'amountDisplayString': renderer_text(item.get('purchaseAmountText', {})),
                               'userComment': renderer_text(item.get('message', {}))}
                    events[item['id']] = {'id': item['id'], 'snippet': {
                        'type': 'superChatEvent' if is_chat else 'superStickerEvent',
                        'publishedAt': published,
                        'superChatDetails' if is_chat else 'superStickerDetails': details},
                        'authorDetails': {'channelId': item.get('authorExternalChannelId'),
                                          'displayName': renderer_text(item.get('authorName', {}))},
                        '_source': 'yt-dlp', '_renderer': item}
                    if kind == 'liveChatTextMessageRenderer':
                        events[item['id']]['snippet'] = {
                            'type': 'textMessageEvent', 'publishedAt': published,
                            'textMessageDetails': {'messageText': renderer_text(item.get('message', {}))}}
                else:
                    walk(item)
    walk(action)
    return list(events.values())


def ingest_chat_lines(store, broadcast, stream):
    """Only consume complete lines, leaving partially written JSON for the next read."""
    count = 0
    while True:
        position = stream.tell()
        line = stream.readline()
        if not line or not line.endswith(b'\n'):
            stream.seek(position)
            break
        events = website_events(json.loads(line))
        count += store.save_batch(broadcast, events, None)
    return count


def listen_ytdlp(store, broadcast):
    """Use an isolated downloader process so stopping cannot leave a hanging chat thread."""
    failures = 0
    last_received=None
    def health(status,error=None):
        store.set_state('listener:'+broadcast['channel_id'],json.dumps({
            'video_id':broadcast['video_id'],'status':status,'error':error,
            'last_received_at':last_received,'checked_at':utcnow()}))
    while not STOP.is_set():
        try:
            health('Connecting')
            with tempfile.TemporaryDirectory(prefix='youtube-chat-') as temp:
                target = Path(temp) / 'chat.%(ext)s'
                process = subprocess.Popen([
                    sys.executable, '-m', 'yt_dlp', '--skip-download', '--write-subs',
                    '--sub-langs', 'live_chat', '--ignore-no-formats-error', '--no-cache-dir',
                    '--no-part', '--no-continue', '--socket-timeout', '20', '--retries', '1',
                    '--fragment-retries', '1', '--quiet', '-o', str(target),
                    '--match-filter', 'live_status = is_live',
                    'https://www.youtube.com/watch?v=' + broadcast['video_id']],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                stream = None
                LOG.info('Listening without an API key: %s', broadcast['video_id'])
                try:
                    while True:
                        # yt-dlp inserts the live_chat language before the extension.
                        raw = Path(temp) / 'chat.live_chat.json'
                        if stream is None and raw.exists():
                            stream = raw.open('rb')
                        if stream is not None:
                            position=stream.tell()
                            count = ingest_chat_lines(store, broadcast, stream)
                            if stream.tell()!=position:
                                last_received=utcnow(); health('Receiving')
                            if count:
                                LOG.info('Saved %s paid events from %s', count, broadcast['video_id'])
                        if process.poll() is not None or STOP.wait(1):
                            break
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                    if stream is not None:
                        try:
                            ingest_chat_lines(store, broadcast, stream)
                        finally:
                            stream.close()
                if STOP.is_set():
                    health('Stopped')
                    return
                # A downloader exit is not proof that the broadcast ended.
                result = check_live_page(broadcast['channel_id'])
                store.record_public_result(broadcast['channel_id'], result)
                if result['status'] == 'offline' or result.get('video_id') != broadcast['video_id']:
                    # A different /live target can mean simultaneous streams; do not mark ended.
                    LOG.info('Chat exited for %s; waiting for discovery before retry', broadcast['video_id'])
                    health('Disconnected')
                    return
        except Exception as exc:
            health('Retrying','Chat connection failed ('+type(exc).__name__+'). Retrying automatically.')
            LOG.warning('Website chat %s failed (%s); retrying with recent history',
                        broadcast['video_id'], type(exc).__name__)
        failures += 1
        health('Retrying','Chat feed disconnected. Reconnecting with recent history; some messages may be missed.')
        LOG.warning('Reconnecting %s; recent history may overlap or leave gaps', broadcast['video_id'])
        STOP.wait(min(300, 2 ** min(failures, 8)))


def run_ytdlp(config_path, config):
    store = Store(config_path.parent / config['database'], config_path.parent / config['chat_database'])
    channels = set()
    for name in dict.fromkeys(config['channels']):
        cached = store.rows('SELECT channel_id FROM channels WHERE input=?', (name,))
        channel_id = cached[0]['channel_id'] if cached else resolve_public(name)
        store.execute('INSERT OR REPLACE INTO channels VALUES (?,?)', (name, channel_id))
        if (config_path.parent/'settings.sqlite3').exists():
            from preferences import Preferences
            with closing(Preferences(config_path.parent).connect()) as db, db:
                db.execute('UPDATE OR IGNORE streamers SET channel_id=? WHERE name=?',(channel_id,name))
        channels.add(channel_id)
        LOG.info('Watching without an API key: %s (%s)', name, channel_id)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(32,len(channels)*2+8))
    watchers = [pool.submit(watch_live_page, store, cid, config['public_check_interval_seconds']) for cid in channels]
    workers = {}
    try:
        while not STOP.is_set():
            WAKE.clear()
            for cid in channels:
                state = store.get_state('public-check:' + cid)
                if not state:
                    continue
                found = json.loads(state)
                if found['status'] != 'is_live':
                    continue
                video_id = found['video_id']
                worker = workers.get(video_id)
                if worker is None or worker.done():
                    if worker is not None and worker.exception():
                        LOG.error('Chat worker failed for %s', video_id)
                    broadcast = {'video_id': video_id, 'channel_id': cid,
                                 'chat_id': 'website:' + video_id, 'title': found.get('title', '')}
                    workers[video_id] = pool.submit(listen_ytdlp, store, broadcast)
            if any(w.done() and w.exception() for w in watchers):
                raise RuntimeError('A public discovery worker stopped; restart the collector')
            WAKE.wait(5)
    except KeyboardInterrupt:
        LOG.info('Stopping collector')
    finally:
        STOP.set()
        pool.shutdown(wait=True, cancel_futures=True)


class YouTube:
    def __init__(self, key):
        self.key = key

    def get(self, resource, **params):
        import requests
        try:
            response = requests.get('https://www.googleapis.com/youtube/v3/' + resource,
                                    params=params, headers={'x-goog-api-key': self.key}, timeout=30)
        except requests.RequestException:
            raise APIError(0, 'network failure') from None
        if not response.ok:
            try:
                reason = response.json()['error'].get('errors', [{}])[0].get('reason', 'request rejected')
            except (ValueError, KeyError, IndexError, TypeError):
                reason = 'request rejected'
            raise APIError(response.status_code, reason)
        return response.json()

    def resolve(self, name):
        name = name.strip().rstrip('/')
        if '/channel/' in name:
            name = name.split('/channel/')[-1]
        elif '/@' in name:
            name = '@' + name.split('/@')[-1].split('/')[0]
        selector = {'id': name} if name.startswith('UC') else {'forHandle': name}
        items = self.get('channels', part='id', **selector).get('items', [])
        if not items:
            raise ValueError(f'Channel not found: {name}')
        return items[0]['id']

    def discover(self, channel_id):
        # Both queries are needed: discover scheduled videos as well as already-live videos.
        results = []
        for kind in ('live', 'upcoming'):
            token = None
            while True:
                params = dict(part='snippet', channelId=channel_id, type='video',
                              eventType=kind, maxResults=50)
                if token:
                    params['pageToken'] = token
                result = self.get('search', **params)
                results.extend(result.get('items', []))
                token = result.get('nextPageToken')
                if not token:
                    break
        return results

    def videos(self, ids):
        return self.get('videos', part='snippet,liveStreamingDetails', id=','.join(ids)).get('items', [])


def proto_event(message):
    from google.protobuf.json_format import MessageToDict
    event = MessageToDict(message)
    # Protobuf JSON renders enum names in UPPER_SNAKE_CASE, unlike REST.
    kind = event.get('snippet', {}).get('type')
    mapped = {'TEXT_MESSAGE_EVENT': 'textMessageEvent', 'SUPER_CHAT_EVENT': 'superChatEvent', 'SUPER_STICKER_EVENT': 'superStickerEvent',
              'CHAT_ENDED_EVENT': 'chatEndedEvent'}
    if kind in mapped:
        event['snippet']['type'] = mapped[kind]
    return event


def listen(store, api, broadcast):
    import grpc
    import stream_list_pb2 as pb
    import stream_list_pb2_grpc as rpc
    failures = 0
    with grpc.secure_channel('youtube.googleapis.com:443', grpc.ssl_channel_credentials()) as channel:
        stub = rpc.V3DataLiveChatMessageServiceStub(channel)
        while not STOP.is_set():
            call = None
            try:
                request = pb.LiveChatMessageListRequest(
                    live_chat_id=broadcast['chat_id'], part=['id', 'snippet', 'authorDetails'])
                token = store.token(broadcast['chat_id'])
                if token:
                    request.page_token = token
                call = stub.StreamList(request, metadata=(('x-goog-api-key', api.key),))
                with CALLS_LOCK:
                    if STOP.is_set():
                        call.cancel()
                        return
                    CALLS.add(call)
                LOG.info('Listening: %s (%s)', broadcast['title'], broadcast['video_id'])
                for batch in call:
                    if STOP.is_set():
                        return
                    events = [proto_event(item) for item in batch.items]
                    count = store.save_batch(broadcast, events, batch.next_page_token)
                    if count:
                        LOG.info('Saved %s paid events from %s', count, broadcast['video_id'])
                    failures = 0
                    if any(e.get('snippet', {}).get('type') == 'chatEndedEvent' for e in events):
                        store.execute("UPDATE broadcasts SET status='ended',updated_at=? WHERE video_id=?",
                                      (utcnow(), broadcast['video_id']))
                        return
                # A normal connection close is not sufficient evidence that the broadcast ended.
            except grpc.RpcError as exc:
                if STOP.is_set():
                    return
                code = exc.code()
                LOG.warning('Chat %s disconnected: %s', broadcast['video_id'], code.name)
                if code in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND):
                    try:
                        videos = api.videos([broadcast['video_id']])
                        if videos and videos[0].get('liveStreamingDetails', {}).get('actualEndTime'):
                            store.execute("UPDATE broadcasts SET status='ended',updated_at=? WHERE video_id=?",
                                          (utcnow(), broadcast['video_id']))
                            return
                    except APIError:
                        pass
                if code == grpc.StatusCode.INVALID_ARGUMENT and store.token(broadcast['chat_id']):
                    # Only discard a resume token if the server explicitly identifies it as invalid.
                    if 'pagetoken' in (exc.details() or '').lower():
                        store.execute('DELETE FROM cursors WHERE chat_id=?', (broadcast['chat_id'],))
                        LOG.error('Resume token rejected for %s; restarting with recent history; a gap may exist', broadcast['video_id'])
                if code in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.UNAUTHENTICATED,
                            grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.FAILED_PRECONDITION):
                    STOP.wait(600)
            except Exception as exc:
                # Avoid logging request objects or credentials from third-party exceptions.
                LOG.error('Listener %s failed (%s); retrying', broadcast['video_id'], type(exc).__name__)
            finally:
                if call is not None:
                    call.cancel()
                    with CALLS_LOCK:
                        CALLS.discard(call)
            failures += 1
            STOP.wait(min(300, 2 ** min(failures, 8)) + random.random())


def load_config(path):
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config.get('channels'),list) or not all(isinstance(c, str) and c for c in config['channels']):
        raise ValueError('config.channels must be a nonempty list of handles or channel IDs')
    config.setdefault('database', 'superchats.sqlite3')
    config.setdefault('chat_database', 'chat-messages.sqlite3')
    config.setdefault('mode', 'ytdlp')
    if config['mode'] not in ('ytdlp', 'official'):
        raise ValueError('mode must be ytdlp or official')
    config.setdefault('discovery_interval_seconds', 3600)
    config.setdefault('video_check_interval_seconds', 30)
    config.setdefault('public_check_interval_seconds', 60)
    for field in ('discovery_interval_seconds', 'video_check_interval_seconds', 'public_check_interval_seconds'):
        if config[field] < 1:
            raise ValueError(field + ' must be positive')
    if (path.parent/'settings.sqlite3').exists():
        from preferences import Preferences
        config=Preferences(path.parent).config(config,active=True)
    return config


def run(config_path):
    config = load_config(config_path)
    if config['mode'] == 'ytdlp':
        return run_ytdlp(config_path, config)
    key = os.environ.get('YOUTUBE_API_KEY', '').strip()
    if not key:
        raise ValueError('Set the YOUTUBE_API_KEY environment variable before running')
    store = Store(config_path.parent / config['database'], config_path.parent / config['chat_database'])
    api = YouTube(key)
    channel_ids = set()
    for name in dict.fromkeys(config['channels']):
        rows = store.rows('SELECT channel_id FROM channels WHERE input=?', (name,))
        channel_id = rows[0]['channel_id'] if rows else api.resolve(name)
        store.execute('INSERT OR REPLACE INTO channels VALUES (?,?)', (name, channel_id))
        channel_ids.add(channel_id)
        LOG.info('Watching %s (%s)', name, channel_id)
    LOG.info('Database: %s', store.path)
    workers = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=32)
    public_pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(channel_ids))
    public_workers = [public_pool.submit(watch_live_page, store, channel_id,
                      config['public_check_interval_seconds']) for channel_id in channel_ids]
    retry_discovery = {}
    try:
        while not STOP.is_set():
            WAKE.clear()
            now = time.time()
            for channel_id in channel_ids:
                state_key = 'discovery:' + channel_id
                last = float(store.get_state(state_key) or 0)
                if now < retry_discovery.get(channel_id, 0) or now - last < config['discovery_interval_seconds']:
                    continue
                try:
                    for item in api.discover(channel_id):
                        store.upsert_video(item['id']['videoId'], channel_id, item['snippet'].get('title', ''))
                    store.set_state(state_key, now)
                except APIError as exc:
                    LOG.warning('Discovery for %s: %s', channel_id, exc)
                    retry_discovery[channel_id] = now + (3600 if exc.status in (400, 401, 403, 429) else 60)
            candidates = [b for b in store.rows("SELECT * FROM broadcasts WHERE status!='ended'")
                          if b['channel_id'] in channel_ids]
            for start in range(0, len(candidates), 50):
                try:
                    videos = api.videos([b['video_id'] for b in candidates[start:start + 50]])
                    for video in videos:
                        details = video.get('liveStreamingDetails', {})
                        chat_id = details.get('activeLiveChatId')
                        # Check actualStartTime as scheduled broadcasts may expose chat before going live.
                        is_live = video.get('snippet', {}).get('liveBroadcastContent') == 'live'
                        if details.get('actualEndTime'):
                            store.execute("UPDATE broadcasts SET status='ended',updated_at=? WHERE video_id=?",
                                          (utcnow(), video['id']))
                        elif chat_id and is_live:
                            store.execute("UPDATE broadcasts SET status='live',chat_id=?,updated_at=? WHERE video_id=?",
                                          (chat_id, utcnow(), video['id']))
                except APIError as exc:
                    LOG.warning('Video status check: %s', exc)
                    if exc.status in (400, 401, 403, 429):
                        STOP.wait(600)
                    break
            for broadcast in store.rows("SELECT * FROM broadcasts WHERE status='live' AND chat_id IS NOT NULL"):
                if broadcast['channel_id'] not in channel_ids:
                    continue
                video_id = broadcast['video_id']
                old = workers.get(video_id)
                if old is None or old.done():
                    if old is not None and old.exception():
                        LOG.error('Listener stopped unexpectedly for %s', video_id)
                    workers[video_id] = pool.submit(listen, store, api, broadcast)
            for worker in public_workers:
                if worker.done() and worker.exception():
                    raise RuntimeError('A public discovery worker stopped; restart the collector') from worker.exception()
            WAKE.wait(config['video_check_interval_seconds'])
    except KeyboardInterrupt:
        LOG.info('Stopping collector')
    finally:
        STOP.set()
        with CALLS_LOCK:
            for call in list(CALLS):
                call.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        public_pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--export', type=Path, help='Export saved paid events to JSON and exit; no API key needed')
    parser.add_argument('--export-chat', type=Path, help='Export normal chat from its separate database to JSON and exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.export_chat:
            config = load_config(args.config)
            rows = ChatStore(args.config.parent / config['chat_database']).rows()
            args.export_chat.parent.mkdir(parents=True, exist_ok=True)
            args.export_chat.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
            LOG.info('Exported %s normal chat messages to %s', len(rows), args.export_chat)
        elif args.export:
            config = load_config(args.config)
            store = Store(args.config.parent / config['database'])
            rows = store.rows('SELECT * FROM superchats ORDER BY published_at,message_id')
            args.export.parent.mkdir(parents=True, exist_ok=True)
            args.export.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
            LOG.info('Exported %s events to %s', len(rows), args.export)
        else:
            run(args.config)
    except (ValueError, APIError, OSError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
