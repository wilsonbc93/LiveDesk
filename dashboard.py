"""Read-only local dashboard for the YouTube chat collector."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import sqlite3
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
import webbrowser
import secrets
import hashlib
from preferences import Preferences

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'
STOP_FILE = ROOT / 'dashboard.stop'
SETTINGS_TOKEN=secrets.token_urlsafe(32)
SETTINGS_LOCK=threading.Lock()


def read_rows(path, sql, params=()):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, params)]


def usd_cents(row):
    """Use structured USD when available; treat an otherwise unqualified $ as USD."""
    currency = (row.get('currency') or '').upper()
    if currency and currency != 'USD':
        return None
    if currency == 'USD' and row.get('amount_micros') is not None:
        micros = int(row['amount_micros'])
        return micros // 10000 if micros >= 0 and micros % 10000 == 0 else None
    display = (row.get('amount_display') or '').strip()
    number = r'((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)'
    match = re.fullmatch(r'(?:US\s*\$|USD\s*\$?|\$)\s*' + number, display, re.IGNORECASE)
    if not match:
        match = re.fullmatch(number + r'\s*USD', display, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(Decimal(match.group(1).replace(',', '')) * 100)
    except InvalidOperation:
        return None


def donation_summaries(path, video_ids):
    summaries = {vid: {'total_cents': 0, 'payment_count': 0, 'top_donors': []} for vid in video_ids}
    if not video_ids:
        return summaries
    slots = ','.join('?' for _ in video_ids)
    rows = read_rows(path, f'''SELECT message_id,video_id,donor_channel_id,donor_name,
        amount_micros,currency,amount_display FROM superchats WHERE video_id IN ({slots})
        ORDER BY COALESCE(published_at,received_at),message_id''', list(video_ids))
    donors = {vid: {} for vid in video_ids}
    for row in rows:
        cents = usd_cents(row)
        if cents is None:
            continue
        summary = summaries[row['video_id']]
        summary['total_cents'] += cents
        summary['payment_count'] += 1
        name = row['donor_name'] or 'Unknown donor'
        key = row['donor_channel_id'] or ('name:' + row['donor_name'] if row['donor_name'] else 'message:' + row['message_id'])
        donor = donors[row['video_id']].setdefault(key, {'channel_id': row['donor_channel_id'],
                                                        'name': name, 'total_cents': 0})
        donor['name'] = name
        donor['total_cents'] += cents
    for vid, summary in summaries.items():
        summary['top_donors'] = sorted(donors[vid].values(), key=lambda d: (-d['total_cents'], d['name'], d['channel_id'] or ''))[:3]
    return summaries


def snapshot(root, channel=''):
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    preferences=None
    if (root/'settings.sqlite3').exists():
        preferences=Preferences(root).read()
        config=Preferences(root).config(config)
    paid_path = root / config.get('database', 'superchats.sqlite3')
    chat_path = root / config.get('chat_database', 'chat-messages.sqlite3')
    resolved = {r['input']: r['channel_id'] for r in read_rows(paid_path, 'SELECT * FROM channels')}
    state = {r['key']: r['value'] for r in read_rows(paid_path, "SELECT * FROM state WHERE key LIKE 'public-check:%'")}
    now = datetime.now(timezone.utc)
    channels = []
    for name in config['channels']:
        cid = resolved.get(name)
        found = json.loads(state.get('public-check:' + str(cid), '{}'))
        checked = found.get('checked_at')
        age = None
        if checked:
            age = max(0, (now - datetime.fromisoformat(checked)).total_seconds())
        status = found.get('status', 'unknown')
        if age is None or age > max(180, config.get('public_check_interval_seconds', 60) * 3):
            status = 'unknown'
        if preferences and (preferences['options']['paused'] or any(s['name']==name and not s['enabled'] for s in preferences['streamers'])):
            status='paused'
        def nonnegative_number(key):
            value = found.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
        started = nonnegative_number('live_started_at') if status == 'is_live' else None
        elapsed = int(now.timestamp() - started) if started and started <= now.timestamp() else None
        channels.append({'name': name, 'channel_id': cid, 'status': status,
                         'title': found.get('title'), 'video_id': found.get('video_id'),
                         'checked_at': checked, 'age_seconds': age,
                         'concurrent_view_count': nonnegative_number('concurrent_view_count') if status == 'is_live' else None,
                         'view_count': nonnegative_number('view_count') if status == 'is_live' else None,
                         'live_started_at': started, 'live_elapsed_seconds': elapsed})
    allowed = {c['channel_id'] for c in channels if c['channel_id']}
    if channel and channel not in allowed:
        raise ValueError('Unknown channel')
    summaries = donation_summaries(paid_path, {c['video_id'] for c in channels
                                             if c['status'] == 'is_live' and c['video_id']})
    for entry in channels:
        entry['donations'] = summaries.get(entry['video_id'])
    live_ids = [c['video_id'] for c in channels if c['status'] == 'is_live'
                and c['video_id'] and (not channel or channel == c['channel_id'])]
    chat = []
    if live_ids:
        slots = ','.join('?' for _ in live_ids)
        chat = read_rows(chat_path, f'''SELECT message_id,video_id,streamer_channel_id,
            author_name,author_channel_id,message,published_at,received_at FROM chat_messages
            WHERE video_id IN ({slots}) ORDER BY COALESCE(published_at,received_at) DESC,rowid DESC LIMIT 200''', live_ids)
        chat.reverse()
    filters, params = '', []
    if channel:
        filters, params = ' WHERE streamer_channel_id=?', [channel]
    paid = read_rows(paid_path, '''SELECT message_id,video_id,streamer_channel_id,donor_name,
        donor_channel_id,event_type,amount_display,message,published_at,received_at
        FROM superchats''' + filters + ' ORDER BY COALESCE(published_at,received_at) DESC,rowid DESC LIMIT 60', params)
    counts = {}
    for key, path, table in [('chat', chat_path, 'chat_messages'), ('paid', paid_path, 'superchats')]:
        rows = read_rows(path, f'SELECT COUNT(*) AS total FROM {table}')
        counts[key] = rows[0]['total'] if rows else 0
    collector_running = None
    if sys.platform == 'win32':
        import control
        collector_running = control.running()
    return {'app': 'superchat-dashboard','instance':hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest(), 'time': now.isoformat(), 'channels': channels,
            'chat': chat, 'superchats': paid, 'counts': counts, 'collector_running': collector_running,
            'paused':preferences['options']['paused'] if preferences else False,
            'dashboard_refresh_seconds':config.get('dashboard_refresh_seconds',2)}


def history_catalog(root):
    config = json.loads((root/'config.json').read_text(encoding='utf-8'))
    paid_path = root/config.get('database','superchats.sqlite3')
    chat_path = root/config.get('chat_database','chat-messages.sqlite3')
    names = {r['channel_id']:r['input'] for r in read_rows(paid_path,'SELECT * FROM channels')}
    broadcasts = {r['video_id']:r for r in read_rows(paid_path,'SELECT * FROM broadcasts')}
    current = {}
    checks = {}
    for row in read_rows(paid_path,"SELECT * FROM state WHERE key LIKE 'public-check:%'"):
        state = json.loads(row['value'])
        checks[row['key'].split(':',1)[1]] = state.get('status')
        if state.get('video_id') and state.get('status') in ('is_live','is_upcoming'):
            current[state['video_id']] = state['status']
    records = {}
    for kind,path,table in [('chat_count',chat_path,'chat_messages'),('paid_count',paid_path,'superchats')]:
        rows=read_rows(path,f'''SELECT video_id,streamer_channel_id,COUNT(*) AS count,
            MIN(COALESCE(published_at,received_at)) AS first_at,
            MAX(COALESCE(published_at,received_at)) AS last_at FROM {table}
            GROUP BY video_id,streamer_channel_id''')
        for row in rows:
            vid=row['video_id']
            if vid in current:
                continue
            if broadcasts.get(vid,{}).get('status')=='live' and checks.get(row['streamer_channel_id'])!='offline':
                continue  # A failed status check is not evidence the live stream ended.
            item=records.setdefault(vid,{'video_id':vid,'channel_id':row['streamer_channel_id'],
                'chat_count':0,'paid_count':0,'first_at':row['first_at'],'last_at':row['last_at']})
            item[kind]+=row['count']
            item['first_at']=min(item['first_at'],row['first_at'])
            item['last_at']=max(item['last_at'],row['last_at'])
    for vid,item in records.items():
        saved=broadcasts.get(vid,{})
        started=saved.get('live_started_at')
        item.update(streamer=names.get(item['channel_id'],item['channel_id']),
                    title=saved.get('title') or 'Untitled stream · '+vid,
                    started_at=datetime.fromtimestamp(started,timezone.utc).isoformat() if started else None,
                    observed_offline_at=saved.get('ended_at'))
        item['date_at']=item['started_at'] or item['first_at']
    return sorted(records.values(),key=lambda r:(r['date_at'],r['video_id']),reverse=True),paid_path,chat_path,names


def page_number(query,key,default,maximum):
    value=int(query.get(key,[str(default)])[0])
    if value<0 or value>maximum:
        raise ValueError('Invalid pagination')
    return value


def history_list(root,query):
    records,paid_path,_,names=history_catalog(root)
    channel=query.get('channel',[''])[0]
    search=query.get('q',[''])[0].strip().casefold()
    start=query.get('from',[''])[0]; end=query.get('to',[''])[0]
    start_dt=datetime.fromisoformat(start.replace('Z','+00:00')) if start else None
    end_dt=datetime.fromisoformat(end.replace('Z','+00:00')) if end else None
    if (start_dt and not start_dt.tzinfo) or (end_dt and not end_dt.tzinfo):
        raise ValueError('Date filters require timezone information')
    filtered=[]
    for record in records:
        when=datetime.fromisoformat(record['date_at'].replace('Z','+00:00'))
        if channel and record['channel_id']!=channel: continue
        if search and search not in (record['title']+' '+record['streamer']+' '+record['video_id']).casefold(): continue
        if start_dt and when<start_dt: continue
        if end_dt and when>=end_dt: continue
        filtered.append(record)
    offset=page_number(query,'offset',0,1000000000)
    limit=page_number(query,'limit',20,100) or 20
    page=filtered[offset:offset+limit]
    summaries=donation_summaries(paid_path,[r['video_id'] for r in page])
    for item in page: item['donations']=summaries[item['video_id']]
    return {'streams':page,'total':len(filtered),'offset':offset,'limit':limit,
            'channels':[{'channel_id':cid,'name':name} for cid,name in sorted(names.items(),key=lambda pair:pair[1].casefold())]}


def history_detail(root,query):
    records,paid_path,_,_=history_catalog(root)
    vid=query.get('video',[''])[0]
    stream=next((r for r in records if r['video_id']==vid),None)
    if stream is None: raise ValueError('Archived stream not found')
    stream['donations']=donation_summaries(paid_path,[vid])[vid]
    return stream


def history_messages(root,query):
    stream=history_detail(root,query)
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    kind=query.get('kind',['chat'])[0]
    if kind not in ('chat','superchats'): raise ValueError('Invalid message type')
    if kind=='chat':
        path=root/config.get('chat_database','chat-messages.sqlite3'); table='chat_messages'
        fields='message_id,author_name,author_channel_id,message,published_at,received_at'
        author='author_name'
    else:
        path=root/config.get('database','superchats.sqlite3'); table='superchats'
        fields='message_id,donor_name,donor_channel_id,event_type,amount_display,message,published_at,received_at'
        author='donor_name'
    where=' WHERE video_id=?'; params=[stream['video_id']]
    search=query.get('q',[''])[0].strip()
    if search:
        where+=' AND instr(lower(COALESCE(message,\'\')),lower(?))>0'
        params.append(search)
    username=query.get('username',[''])[0].strip().lstrip('@')
    if username:
        where+=f' AND instr(lower(COALESCE({author},\'\')),lower(?))>0'
        params.append(username)
    count=read_rows(path,f'SELECT COUNT(*) AS total FROM {table}'+where,params)
    offset=page_number(query,'offset',0,1000000000)
    limit=page_number(query,'limit',100,200) or 100
    rows=read_rows(path,f'SELECT {fields} FROM {table}'+where+
                  ' ORDER BY COALESCE(published_at,received_at),message_id LIMIT ? OFFSET ?',params+[limit,offset])
    return {'messages':rows,'total':count[0]['total'] if count else 0,'offset':offset,'limit':limit,'kind':kind}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if (self.headers.get('Host') not in ('127.0.0.1:8765','localhost:8765')
            or self.headers.get('Origin') not in (URL,'http://localhost:8765')
            or self.headers.get('X-Settings-Token')!=SETTINGS_TOKEN
            or self.headers.get('Content-Type')!='application/json'):
            self.send_error(403); return
        if self.path!='/api/settings': self.send_error(404); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192: raise ValueError('Invalid request size.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('Invalid settings.')
            from settings_api import change
            with SETTINGS_LOCK: result=change(ROOT,data)
            self.send_json(result)
        except ValueError as exc: self.send_json({'error':str(exc)},400)
        except Exception:
            self.send_json({'error':'Could not save. Check the handle or channel URL and try again.'},503)

    def send_json(self,data,status=200):
        body=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        # Do not serve arbitrary paths or enable cross-origin database access.
        if self.headers.get('Host') not in ('127.0.0.1:8765', 'localhost:8765'):
            self.send_error(403); return
        parsed = urlparse(self.path)
        try:
            query=parse_qs(parsed.query)
            if parsed.path=='/api/settings':
                from settings_api import settings_data
                self.send_json({**settings_data(ROOT),'token':SETTINGS_TOKEN}); return
            elif parsed.path=='/api/diagnostics':
                from diagnostics import report
                self.send_json(report(ROOT)); return
            elif parsed.path=='/api/diagnostics/download':
                from diagnostics import bundle
                body=bundle(ROOT)
                self.send_response(200); self.send_header('Content-Type','application/zip')
                self.send_header('Content-Disposition','attachment; filename="LiveDesk-diagnostics.zip"')
                self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
                self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(body); return
            elif parsed.path=='/api/export':
                from settings_api import export_query,csv_lines
                path,fields,sql,params=export_query(ROOT,query)
                self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename="'+('chat' if query.get('kind',['chat'])[0]=='chat' else 'superchats')+'-export.csv"')
                self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers()
                for chunk in csv_lines(path,fields,sql,params): self.wfile.write(chunk)
                return
            elif parsed.path in ('/api/history','/api/stream','/api/messages'):
                action={'/api/history':history_list,'/api/stream':history_detail,'/api/messages':history_messages}[parsed.path]
                body=json.dumps(action(ROOT,query),ensure_ascii=False).encode('utf-8')
                content_type='application/json; charset=utf-8'
            elif parsed.path == '/api/snapshot':
                channel = parse_qs(parsed.query).get('channel', [''])[0]
                body = json.dumps(snapshot(ROOT, channel), ensure_ascii=False).encode('utf-8')
                content_type = 'application/json; charset=utf-8'
            elif parsed.path in ('/', '/index.html'):
                body = (ROOT / 'dashboard.html').read_bytes()
                content_type = 'text/html; charset=utf-8'
            elif parsed.path in ('/history','/history.html'):
                body=(ROOT/'history.html').read_bytes()
                content_type='text/html; charset=utf-8'
            elif parsed.path in ('/settings','/settings.html'):
                body=(ROOT/'settings.html').read_bytes()
                content_type='text/html; charset=utf-8'
            elif parsed.path in ('/help','/help.html'):
                body=(ROOT/'help.html').read_bytes(); content_type='text/html; charset=utf-8'
            elif parsed.path=='/dashboard.css':
                html=(ROOT/'dashboard.html').read_text(encoding='utf-8')
                body=html.split('<style>',1)[1].split('</style>',1)[0].encode('utf-8')
                content_type='text/css; charset=utf-8'
            else:
                self.send_error(404); return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers(); self.wfile.write(body)
        except ValueError:
            self.send_error(400, 'Invalid channel or configuration')
        except (OSError, sqlite3.Error):
            self.send_error(503, 'Database temporarily unavailable; retry shortly')

    def log_message(self, format, *args):
        pass


def available():
    try:
        with urlopen(URL + '/api/snapshot', timeout=3) as response:
            data=json.load(response)
            return (data.get('app') == 'superchat-dashboard' and
                    data.get('instance')==hashlib.sha256(str(ROOT.resolve()).casefold().encode()).hexdigest())
    except Exception:
        return False


def serve():
    server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    STOP_FILE.unlink(missing_ok=True)
    def monitor():
        while True:
            time.sleep(1)
            if STOP_FILE.exists():
                server.shutdown(); return
    threading.Thread(target=monitor, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close(); STOP_FILE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['open', 'start', 'run', 'stop'], default='open')
    action = parser.parse_args().action
    if action == 'run':
        serve(); return
    if action == 'stop':
        if available():
            STOP_FILE.touch(); print('Dashboard stop requested.')
        else:
            print('Dashboard is stopped.')
        return
    if not available():
        with (ROOT / 'dashboard.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(40):
            if available():
                break
            if process.poll() is not None:
                raise SystemExit('Dashboard could not start. See dashboard.log; port 8765 may be in use.')
            time.sleep(0.25)
        else:
            raise SystemExit('Dashboard startup was not confirmed. See dashboard.log.')
    print('Dashboard: ' + URL)
    if action=='open': webbrowser.open(URL)


if __name__ == '__main__':
    main()
