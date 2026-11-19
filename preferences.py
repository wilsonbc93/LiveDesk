"""Persistent user preferences, migrated once from the original config file."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3

DEFAULTS={'public_check_interval_seconds':60,'dashboard_refresh_seconds':2,
          'chat_retention_days':0,'paid_retention_days':0,'paused':False,'revision':0}


class Preferences:
    def __init__(self,root):
        self.root=Path(root)
        self.path=self.root/'settings.sqlite3'
        with closing(self.connect()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS options(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS streamers(channel_id TEXT PRIMARY KEY,name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,record_chat INTEGER NOT NULL DEFAULT 1,
                record_paid INTEGER NOT NULL DEFAULT 1);''')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM options WHERE key='initialized'").fetchone():
                config=json.loads((self.root/'config.json').read_text(encoding='utf-8'))
                paid=self.root/config.get('database','superchats.sqlite3')
                resolved={}
                if paid.exists():
                    with closing(sqlite3.connect(paid)) as old:
                        if old.execute("SELECT 1 FROM sqlite_master WHERE name='channels'").fetchone():
                            resolved=dict(old.execute('SELECT input,channel_id FROM channels'))
                for name in config.get('channels',[]):
                    # Resolve uncached handles on collector startup, without blocking page loads.
                    db.execute('INSERT OR IGNORE INTO streamers(channel_id,name) VALUES (?,?)',(resolved.get(name,name),name))
                values={**DEFAULTS,'public_check_interval_seconds':config.get('public_check_interval_seconds',60),'initialized':True}
                db.executemany('INSERT OR REPLACE INTO options VALUES (?,?)',[(k,json.dumps(v)) for k,v in values.items()])

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15); db.row_factory=sqlite3.Row
        return db

    def read(self):
        with closing(self.connect()) as db:
            values={**DEFAULTS,**{r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM options')}}
            return {'options':values,'streamers':[dict(r) for r in db.execute('SELECT * FROM streamers ORDER BY name COLLATE NOCASE')]}

    def mutate(self,action,data,resolver=None):
        if action=='add':
            name=str(data.get('name','')).strip()
            if not name or len(name)>300: raise ValueError('Enter a YouTube handle or channel URL.')
            cid=resolver(name)
            if '/@' in name: name='@'+name.split('/@',1)[1].split('/')[0]
            elif not name.startswith(('@','http','UC')): name='@'+name
        with closing(self.connect()) as db, db:
            if action=='add':
                if db.execute('SELECT 1 FROM streamers WHERE channel_id=?',(cid,)).fetchone():
                    raise ValueError('This streamer is already in your list.')
                db.execute('INSERT INTO streamers(channel_id,name) VALUES (?,?)',(cid,name))
            elif action in ('update','remove'):
                cid=data.get('channel_id')
                if not db.execute('SELECT 1 FROM streamers WHERE channel_id=?',(cid,)).fetchone(): raise ValueError('Streamer not found.')
                if action=='remove': db.execute('DELETE FROM streamers WHERE channel_id=?',(cid,))
                else:
                    flags=[]
                    for key in ('enabled','record_chat','record_paid'):
                        if type(data.get(key)) is not bool: raise ValueError('Recording choices must be switches.')
                        flags.append(int(data[key]))
                    if flags[0] and not any(flags[1:]): raise ValueError('Select at least one recording type, or pause this streamer.')
                    db.execute('UPDATE streamers SET enabled=?,record_chat=?,record_paid=? WHERE channel_id=?',(*flags,cid))
            elif action=='options':
                limits={'public_check_interval_seconds':(15,3600),'dashboard_refresh_seconds':(1,60),
                        'chat_retention_days':(0,36500),'paid_retention_days':(0,36500)}
                for key,(low,high) in limits.items():
                    value=data.get(key)
                    if type(value) is not int or not low<=value<=high: raise ValueError(f'{key}: choose a whole number from {low} to {high}.')
                    db.execute('INSERT OR REPLACE INTO options VALUES (?,?)',(key,json.dumps(value)))
            elif action=='pause':
                if type(data.get('paused')) is not bool: raise ValueError('Invalid pause choice.')
                db.execute("INSERT OR REPLACE INTO options VALUES ('paused',?)",(json.dumps(data['paused']),))
            else: raise ValueError('Unknown settings action.')
            db.execute("UPDATE options SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='revision'")
        return self.read()

    def config(self,base,active=False):
        state=self.read(); result={**base,**state['options']}
        result['channels']=[s['name'] for s in state['streamers'] if not active or (s['enabled'] and (s['record_chat'] or s['record_paid']))]
        return result

    def recording(self,cid):
        state=self.read()
        entry=next((s for s in state['streamers'] if s['channel_id']==cid),None)
        if state['options']['paused'] or not entry or not entry['enabled']: return False,False
        return bool(entry['record_chat']),bool(entry['record_paid'])

    def retain(self):
        """Delete in small transactions; freed pages are reused by subsequent writes."""
        config=json.loads((self.root/'config.json').read_text(encoding='utf-8'))
        options=self.read()['options']
        for setting,key,default,table in [('chat_retention_days','chat_database','chat-messages.sqlite3','chat_messages'),
                                         ('paid_retention_days','database','superchats.sqlite3','superchats')]:
            days=options[setting]; path=self.root/config.get(key,default)
            if not days or not path.exists(): continue
            cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
            with closing(sqlite3.connect(path,timeout=15)) as db:
                while True:
                    with db:
                        count=db.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE received_at<? LIMIT 1000)',(cutoff,)).rowcount
                    if count<1000: break
