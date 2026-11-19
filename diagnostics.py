"""Small diagnostic reports: app health and redacted log tails, never chat databases."""
from datetime import datetime,timezone
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import sys
import zipfile

LOGS=('setup.log','collector.log','dashboard.log','dashboard-out.log','launcher.log')


def redact(text,root):
    paths={str(root),str(Path.home()),os.environ.get('TEMP','')}
    paths|={p.replace('\\','/') for p in paths}|{p.replace('/','\\') for p in paths}
    for path in sorted(paths,key=len,reverse=True):
        if path: text=re.sub(re.escape(path),'<local-folder>',text,flags=re.I)
    text=re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://<credentials>@',text,flags=re.I)
    text=re.sub(r'''(?im)((?:api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|authorization|cookie|secret|password)["']?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,;]+)''',r'\1<redacted>',text)
    return text


def tail(path,maximum=32768):
    if not path.exists(): return ''
    with path.open('rb') as file:
        file.seek(max(0,path.stat().st_size-maximum))
        return file.read(maximum).decode('utf-8',errors='replace')


def report(root):
    from settings_api import settings_data
    from startup import enabled
    import dashboard
    data=settings_data(root)
    versions={}
    for package in ('yt-dlp','requests','grpcio','protobuf'):
        try: versions[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: versions[package]='Not installed'
    checks=[]
    if not data['collector_running']: checks.append('Collector is stopped. Use Start LiveDesk or Resume collection in Settings.')
    if data['options']['paused']: checks.append('Global collection is paused. Resume it in Settings when ready.')
    if not data['streamers']: checks.append('No streamers yet. Add a YouTube handle or channel URL in Settings.')
    for s in data['streamers']:
        h=s['health']; listener=s['listener']
        if not s['enabled']: continue
        if h.get('status')=='error': checks.append(s['name']+': '+h.get('error','YouTube status check failed.'))
        if listener.get('error') and h.get('status')=='is_live': checks.append(s['name']+': '+listener['error'])
        if h.get('checked_at') and data['collector_running'] and not data['options']['paused']:
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(h['checked_at'])).total_seconds()
                if age>max(180,data['options']['public_check_interval_seconds']*3): checks.append(s['name']+': status check is overdue. Check the collector log below.')
            except ValueError: pass
    return {'app':'LiveDesk','version':(root/'VERSION').read_text().strip() if (root/'VERSION').exists() else 'development',
        'time':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
        'windows':platform.system()+' '+platform.release(),'architecture':platform.machine(),
        'dependencies':versions,'collector_running':data['collector_running'],
        'collection_paused':data['options']['paused'],'start_at_login':enabled(root),
        'streamer_count':len(data['streamers']),'enabled_streamers':sum(bool(s['enabled']) for s in data['streamers']),
        'storage':data['storage'],'checks':checks,
        'logs':{name:redact(tail(root/name),root) for name in LOGS}}


def bundle(root):
    data=report(root); logs=data.pop('logs'); output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('health.json',json.dumps(data,indent=2))
        z.writestr('README.txt','Diagnostic report contains health information and redacted log tails. It excludes chat, donations, databases, full configuration, and environment variables. Review before sharing. Nothing is uploaded automatically.\n')
        for name,text in logs.items():
            if text: z.writestr('logs/'+name,text)
    return output.getvalue()
