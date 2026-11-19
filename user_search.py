"""Account-based search across captured normal and paid chat, using read-only databases."""
from contextlib import contextmanager
import json
import sqlite3


def identity(user):
    if not isinstance(user,str) or len(user)>600: raise ValueError('Invalid account')
    prefix,separator,value=user.partition(':')
    if not separator or prefix not in ('id','name') or not value: raise ValueError('Invalid account')
    if prefix=='id': return 'author_channel_id=?',[value]
    return "COALESCE(author_channel_id,'')='' AND author_name=?",[value]


@contextmanager
def database(root):
    from dashboard import usd_cents
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    paid=root/config.get('database','superchats.sqlite3')
    chat=root/config.get('chat_database','chat-messages.sqlite3')
    db=sqlite3.connect(paid.resolve().as_uri()+'?mode=ro' if paid.exists() else ':memory:',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    db.create_function('fold',1,lambda value:(value or '').casefold(),deterministic=True)
    db.create_function('usd',3,lambda currency,micros,display:usd_cents(
        {'currency':currency,'amount_micros':micros,'amount_display':display}),deterministic=True)
    try:
        if chat.exists(): db.execute('ATTACH DATABASE ? AS chats',(chat.resolve().as_uri()+'?mode=ro',))
        def exists(schema,table):
            return db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE name=? AND type='table'",(table,)).fetchone()
        parts=[]
        for schema,table,author,channel,kind in [('main','superchats','donor_name','donor_channel_id','superchats'),
                                               ('chats','chat_messages','author_name','author_channel_id','chat')]:
            if schema=='chats' and not chat.exists(): continue
            if not exists(schema,table): continue
            paid_fields="amount_display,event_type,usd(currency,amount_micros,amount_display)" if kind=='superchats' else "NULL,'textMessageEvent',NULL"
            parts.append(f'''SELECT message_id,video_id,streamer_channel_id,{author} AS author_name,
                NULLIF({channel},'') AS author_channel_id,
                CASE WHEN NULLIF({channel},'') IS NOT NULL THEN 'id:'||{channel} ELSE 'name:'||{author} END AS user_key,
                '{kind}' AS kind,message,strftime('%Y-%m-%dT%H:%M:%fZ',COALESCE(published_at,received_at)) AS at,
                {paid_fields} FROM {schema}.{table}
                WHERE NULLIF({channel},'') IS NOT NULL OR NULLIF({author},'') IS NOT NULL''')
        if not parts:
            parts=["SELECT NULL AS message_id,NULL AS video_id,NULL AS streamer_channel_id,NULL AS author_name,NULL AS author_channel_id,NULL AS user_key,NULL AS kind,NULL AS message,NULL AS at,NULL AS amount_display,NULL AS event_type,NULL AS usd_cents WHERE 0"]
        # Explicit columns keep the normal/paid UNION independent of database field ordering.
        db.execute('CREATE TEMP VIEW events(message_id,video_id,streamer_channel_id,author_name,author_channel_id,user_key,kind,message,at,amount_display,event_type,usd_cents) AS '+' UNION ALL '.join(parts))
        if exists('main','channels'): db.execute('CREATE TEMP VIEW streamer_names AS SELECT channel_id,input AS name FROM main.channels')
        else: db.execute('CREATE TEMP VIEW streamer_names AS SELECT NULL AS channel_id,NULL AS name WHERE 0')
        if exists('main','broadcasts'): db.execute('CREATE TEMP VIEW stream_titles AS SELECT video_id,title FROM main.broadcasts')
        else: db.execute('CREATE TEMP VIEW stream_titles AS SELECT NULL AS video_id,NULL AS title WHERE 0')
        db.execute('BEGIN')
        yield db
    finally: db.close()


TOTALS="""COUNT(*) AS message_count,SUM(kind='chat') AS chat_count,SUM(kind='superchats') AS paid_count,
    COUNT(DISTINCT video_id) AS stream_count,COUNT(DISTINCT streamer_channel_id) AS streamer_count,
    MIN(at) AS first_at,MAX(at) AS last_at,COALESCE(SUM(usd_cents),0) AS total_cents"""


def decorate(db,row):
    item=dict(row)
    latest=db.execute('SELECT author_name,author_channel_id FROM events WHERE user_key=? ORDER BY at DESC,kind,message_id DESC LIMIT 1',(item['user_key'],)).fetchone()
    item['name']=latest['author_name'] or latest['author_channel_id'] or 'Unnamed account'
    item['channel_id']=latest['author_channel_id']
    item['verified']=bool(item['channel_id'])
    return item


def users(root,query):
    from dashboard import page_number
    text=query.get('q',[''])[0].strip().lstrip('@')
    offset=page_number(query,'offset',0,1000000000); limit=page_number(query,'limit',30,100) or 30
    if len(text)<2: return {'users':[],'total':0,'offset':offset,'limit':limit}
    if len(text)>300: raise ValueError('Search is too long')
    with database(root) as db:
        matched="SELECT DISTINCT user_key FROM events WHERE instr(fold(author_name),?)>0 OR author_channel_id=?"
        params=[text.casefold(),text]
        count=db.execute('SELECT COUNT(*) FROM ('+matched+')',params).fetchone()[0]
        rows=db.execute('WITH matched AS ('+matched+') SELECT e.user_key,'+TOTALS+'''
            FROM events e JOIN matched m ON e.user_key=m.user_key GROUP BY e.user_key
            ORDER BY message_count DESC,e.user_key LIMIT ? OFFSET ?''',params+[limit,offset]).fetchall()
        return {'users':[decorate(db,row) for row in rows],'total':count,'offset':offset,'limit':limit}


def profile(root,query):
    user=query.get('user',[''])[0]; where,params=identity(user)
    with database(root) as db:
        row=db.execute('SELECT user_key,'+TOTALS+' FROM events WHERE '+where,params).fetchone()
        if not row['message_count']: raise ValueError('Account not found')
        result=decorate(db,row)
        result['aliases']=[r[0] for r in db.execute('SELECT DISTINCT author_name FROM events WHERE '+where+" AND NULLIF(author_name,'') IS NOT NULL ORDER BY author_name",params)]
        streamers=[]
        for record in db.execute('SELECT streamer_channel_id,'+TOTALS+' FROM events WHERE '+where+' GROUP BY streamer_channel_id ORDER BY message_count DESC,streamer_channel_id',params):
            item=dict(record)
            name=db.execute('SELECT name FROM streamer_names WHERE channel_id=? ORDER BY name LIMIT 1',(item['streamer_channel_id'],)).fetchone()
            item['name']=name[0] if name else item['streamer_channel_id']
            streamers.append(item)
        result['streamers']=streamers
        result['streams']=[dict(r) for r in db.execute('''SELECT e.video_id,e.streamer_channel_id,
            COALESCE(t.title,e.video_id) AS title,MIN(e.at) AS first_at FROM events e
            LEFT JOIN stream_titles t ON e.video_id=t.video_id WHERE '''+where+'''
            GROUP BY e.video_id,e.streamer_channel_id ORDER BY first_at DESC,e.video_id''',params)]
        return result


def messages(root,query):
    from dashboard import page_number
    where,params=identity(query.get('user',[''])[0])
    kind=query.get('kind',['all'])[0]
    if kind not in ('all','chat','superchats'): raise ValueError('Invalid message type')
    if kind!='all': where+=' AND kind=?'; params.append(kind)
    for key,column in [('channel','streamer_channel_id'),('video','e.video_id')]:
        value=query.get(key,[''])[0]
        if value: where+=' AND '+column+'=?'; params.append(value)
    text=query.get('q',[''])[0].strip()
    if len(text)>500: raise ValueError('Search is too long')
    if text: where+=" AND instr(fold(message),?)>0"; params.append(text.casefold())
    from datetime import datetime,timezone
    for key,operator in [('from','>='),('to','<')]:
        value=query.get(key,[''])[0]
        if value:
            date=datetime.fromisoformat(value.replace('Z','+00:00'))
            if date.tzinfo is None: raise ValueError('Date needs timezone')
            where+=' AND julianday(at)'+operator+'julianday(?)'; params.append(date.astimezone(timezone.utc).isoformat())
    offset=page_number(query,'offset',0,1000000000); limit=page_number(query,'limit',100,200) or 100
    with database(root) as db:
        sql=' FROM events e LEFT JOIN stream_titles t ON e.video_id=t.video_id WHERE '+where
        total=db.execute('SELECT COUNT(*)'+sql,params).fetchone()[0]
        rows=[dict(r) for r in db.execute('SELECT e.*,COALESCE(t.title,e.video_id) AS title'+sql+
            ' ORDER BY at DESC,kind,message_id DESC LIMIT ? OFFSET ?',params+[limit,offset])]
        names={r['channel_id']:r['name'] for r in db.execute('SELECT * FROM streamer_names')}
        for row in rows: row['streamer']=names.get(row['streamer_channel_id'],row['streamer_channel_id'])
        return {'messages':rows,'total':total,'offset':offset,'limit':limit}
