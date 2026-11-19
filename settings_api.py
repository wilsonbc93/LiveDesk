"""Local settings, storage information, and bounded-memory CSV exports."""
from contextlib import closing
from datetime import datetime, timezone
import csv
import io
import json
import sqlite3
from preferences import Preferences


def configuration(root):
    return json.loads((root/'config.json').read_text(encoding='utf-8'))


def settings_data(root):
    from dashboard import read_rows
    import control
    prefs=Preferences(root); data=prefs.read(); config=configuration(root)
    paid=root/config.get('database','superchats.sqlite3')
    states={r['key']:json.loads(r['value']) for r in read_rows(paid,"SELECT * FROM state WHERE key LIKE 'public-check:%' OR key LIKE 'listener:%'")}
    for streamer in data['streamers']:
        streamer['health']=states.get('public-check:'+streamer['channel_id'],{})
        streamer['listener']=states.get('listener:'+streamer['channel_id'],{})
    data['collector_running']=control.running()
    data['storage']=[]
    for kind,path,table in [('chat',root/config.get('chat_database','chat-messages.sqlite3'),'chat_messages'),
                             ('superchats',paid,'superchats')]:
        rows=read_rows(path,f'SELECT COUNT(*) AS messages,MIN(received_at) AS oldest,MAX(received_at) AS newest FROM {table}')
        files=[path,path.with_name(path.name+'-wal'),path.with_name(path.name+'-shm')]
        data['storage'].append({'kind':kind,'bytes':sum(p.stat().st_size for p in files if p.exists()),
                                **(rows[0] if rows else {'messages':0,'oldest':None,'newest':None})})
    data['settings_bytes']=prefs.path.stat().st_size
    from startup import enabled
    data['start_at_login']=enabled(root)
    data['export_channels']=read_rows(paid,'SELECT channel_id,input AS name FROM channels ORDER BY input COLLATE NOCASE')
    data['export_streams']=read_rows(paid,'SELECT video_id,title,channel_id FROM broadcasts ORDER BY updated_at DESC')
    return data


def change(root,data):
    from collector import resolve_public,Store
    prefs=Preferences(root); action=data.get('action')
    if action=='startup':
        from startup import set_enabled
        set_enabled(root,data.get('enabled'))
        return {'saved':True,'message':'Login startup preference saved for this Windows account.'}
    result=prefs.mutate(action,data,resolve_public)
    if action=='add':
        config=configuration(root)
        store=Store(root/config.get('database','superchats.sqlite3'))
        for s in result['streamers']:
            store.execute('INSERT OR REPLACE INTO channels VALUES (?,?)',(s['name'],s['channel_id']))
    if action=='pause' and not data['paused']:
        import subprocess,sys,os
        subprocess.run([sys.executable,str(root/'control.py'),'start'],cwd=root,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=True)
    return {'saved':True,'message':'Saved. Collection changes apply automatically.'}


def export_query(root,query):
    kind=query.get('kind',['chat'])[0]
    if kind not in ('chat','superchats'): raise ValueError('Select chat or Super Chats.')
    config=configuration(root)
    if kind=='chat':
        path=root/config.get('chat_database','chat-messages.sqlite3'); table='chat_messages'
        fields='message_id,video_id,streamer_channel_id,author_channel_id,author_name,published_at,received_at,message'
    else:
        path=root/config.get('database','superchats.sqlite3'); table='superchats'
        fields='message_id,video_id,streamer_channel_id,donor_channel_id,donor_name,published_at,received_at,event_type,amount_display,currency,amount_micros,message'
    where=[]; params=[]
    for key,column in [('channel','streamer_channel_id'),('video','video_id')]:
        value=query.get(key,[''])[0]
        if value: where.append(column+'=?'); params.append(value)
    for key,op in [('from','>='),('to','<')]:
        value=query.get(key,[''])[0]
        if value:
            date=datetime.fromisoformat(value.replace('Z','+00:00'))
            if not date.tzinfo: raise ValueError('Dates must include a timezone.')
            where.append(f'julianday(COALESCE(published_at,received_at)){op}julianday(?)')
            params.append(date.astimezone(timezone.utc).isoformat())
    sql='SELECT '+fields+' FROM '+table+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY COALESCE(published_at,received_at),message_id'
    return path,fields.split(','),sql,params


def csv_lines(path,fields,sql,params):
    buffer=io.StringIO(); writer=csv.writer(buffer)
    def encode(row):
        buffer.seek(0); buffer.truncate()
        # Keep downloaded chat text from becoming executable spreadsheet formulas.
        writer.writerow([("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for v in row])
        return buffer.getvalue().encode('utf-8')
    yield b'\xef\xbb\xbf'+encode(fields)
    if not path.exists(): return
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        for row in db.execute(sql,params): yield encode(row)
