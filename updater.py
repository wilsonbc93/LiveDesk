"""Install a user-selected release ZIP with backups and code/environment rollback."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

APP_ID='livedesk-windows'


def allowed_file(name):
    parts=PurePosixPath(name).parts
    if any(p!=p.rstrip(' .') or p.split('.')[0].upper() in
           {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for p in parts): return False
    return (len(parts)==1 and re.fullmatch(r'[A-Za-z0-9_. -]+',name) is not None and
            (Path(name).suffix.lower() in ('.py','.html','.cmd','.txt','.md','.proto') or
             name in ('config.json','config.example.json','.gitignore','VERSION'))) or name=='.github/workflows/windows.yml'


def validate_release(archive):
    with zipfile.ZipFile(archive) as z:
        members={}; seen=set()
        for info in z.infolist():
            path=PurePosixPath(info.filename)
            if ('\\' in info.filename or path.is_absolute() or '..' in path.parts or
                ':' in info.filename or stat.S_ISLNK(info.external_attr>>16)):
                raise ValueError('The archive contains an unsafe path or symbolic link.')
            if info.is_dir(): continue
            if info.filename.casefold() in seen: raise ValueError('Duplicate file in archive.')
            seen.add(info.filename.casefold())
            members[info.filename]=info
        manifests=[name for name in members if name=='release.json' or name.endswith('/release.json')]
        if len(manifests)!=1: raise ValueError('Choose a LiveDesk release ZIP containing release.json, not a generic source-code ZIP.')
        manifest_name=manifests[0]; prefix=manifest_name[:-len('release.json')]
        if members[manifest_name].file_size>200000: raise ValueError('Release manifest is too large.')
        manifest=json.loads(z.read(manifest_name))
        if manifest.get('app')!=APP_ID or not isinstance(manifest.get('version'),str): raise ValueError('This is not a LiveDesk Windows release.')
        files=manifest.get('files',{})
        if not isinstance(files,dict) or not 1<=len(files)<=200: raise ValueError('Invalid file manifest.')
        if len({name.casefold() for name in files})!=len(files): raise ValueError('Duplicate application filename.')
        required={'app.py','bootstrap.py','control.py','collector.py','dashboard.py','preferences.py',
                  'requirements.txt','settings_api.py','settings.html','dashboard.html','history.html','updater.py','VERSION'}
        if not required<=files.keys(): raise ValueError('This release is missing required application files.')
        payload={}
        if sum(i.file_size for i in members.values())>50*1024*1024: raise ValueError('Release ZIP is unexpectedly large.')
        for name,digest in files.items():
            if not allowed_file(name) or prefix+name not in members: raise ValueError('Unexpected or missing application file: '+name)
            body=z.read(prefix+name)
            if hashlib.sha256(body).hexdigest()!=digest: raise ValueError('File checksum mismatch: '+name)
            payload[name]=body
        if {prefix+n for n in files}|{manifest_name} != set(members): raise ValueError('Archive contains files outside the release manifest.')
        return manifest,payload


def move_inside(root,source,target):
    root=root.resolve(); source=source.resolve(); target=target.resolve()
    if not source.is_relative_to(root) or not target.is_relative_to(root) or source==root or target==root:
        raise ValueError('Update paths must stay inside the installation folder.')
    target.parent.mkdir(parents=True,exist_ok=True)
    os.replace(source,target)


def backup_databases(root,destination):
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    paths={'settings':root/'settings.sqlite3','chat':root/config.get('chat_database','chat-messages.sqlite3'),
           'superchats':root/config.get('database','superchats.sqlite3')}
    destination.mkdir(parents=True,exist_ok=True)
    for label,path in paths.items():
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination/(label+'.sqlite3'))) as dst:
                src.backup(dst)


def apply_payload(root,manifest,payload,backup):
    saved={}; created=[]
    (backup/'code').mkdir(parents=True,exist_ok=True)
    for name in payload:
        if name in ('config.json','config.example.json') and (root/name).exists(): continue
        target=root/name
        if not target.resolve().is_relative_to(root.resolve()): raise ValueError('Application files must stay inside the installation folder.')
        saved[name]=target.read_bytes() if target.exists() else None
    # Write the complete rollback snapshot before changing any application file.
    for name,body in saved.items():
        if body is not None:
            p=backup/'code'/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(body)
    if (root/'config.json').exists(): (backup/'config.json').write_bytes((root/'config.json').read_bytes())
    try:
        for name in saved:
            target=root/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(payload[name]); created.append(name)
        (root/'release.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    except Exception:
        rollback_payload(root,saved); raise
    return saved


def rollback_payload(root,saved):
    for name,body in saved.items():
        target=root/name
        if body is None: target.unlink(missing_ok=True)
        else: target.write_bytes(body)


def update(root,archive,prepare_environment=None):
    from bootstrap import prepare,verify
    manifest,payload=validate_release(archive)
    root=root.resolve(); stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup=root/'backups'/stamp
    print('Preparing LiveDesk '+manifest['version']+'. Your current app keeps running during preparation.',flush=True)
    with tempfile.TemporaryDirectory(prefix='.livedesk-update-',dir=root) as temporary:
        stage=Path(temporary); code=stage/'code'; code.mkdir()
        for name,body in payload.items():
            p=code/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(body)
        candidate=stage/'environment'
        try:
            (prepare_environment or prepare)(code,candidate)
        finally:
            if (code/'setup.log').exists():
                with (root/'setup.log').open('a',encoding='utf-8') as log:
                    log.write('\nUpdate preparation '+stamp+'\n'+(code/'setup.log').read_text(encoding='utf-8',errors='replace'))
        import control,dashboard
        was_running=control.running(); had_dashboard=dashboard.available()
        def command(script,action,check=True):
            python=root/'.venv/Scripts/python.exe'
            return subprocess.run([str(python),str(root/script),action],cwd=root,check=check,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,timeout=110)
        command('app.py','stop')
        saved=None; moved=False; old_manifest=(root/'release.json').read_bytes() if (root/'release.json').exists() else None
        try:
            backup_databases(root,backup/'databases')
            saved=apply_payload(root,manifest,payload,backup)
            move_inside(root,root/'.venv',backup/'environment'); moved=True
            move_inside(root,candidate,root/'.venv')
            verify(root,root/'.venv/Scripts/python.exe')
        except Exception:
            if moved:
                if (root/'.venv').exists(): move_inside(root,root/'.venv',stage/'failed-environment')
                move_inside(root,backup/'environment',root/'.venv')
            if saved is not None: rollback_payload(root,saved)
            if old_manifest is None: (root/'release.json').unlink(missing_ok=True)
            else: (root/'release.json').write_bytes(old_manifest)
            if was_running: command('control.py','start',False)
            if had_dashboard: command('dashboard.py','start',False)
            raise
        if was_running: command('control.py','start')
        if had_dashboard: command('dashboard.py','start')
        print('Update complete. Settings and history preserved. Backup: '+str(backup))
    return backup


def update_interactive(root,archive=None):
    if archive is None:
        import tkinter as tk
        from tkinter import filedialog
        window=tk.Tk(); window.withdraw()
        try: selected=filedialog.askopenfilename(title='Select the LiveDesk release ZIP you downloaded',filetypes=[('LiveDesk ZIP','*.zip')])
        finally: window.destroy()
        if not selected: print('Update canceled.'); return
        archive=Path(selected)
    manifest,_=validate_release(archive)
    print('Selected release: '+manifest['version'])
    print('Only use a release downloaded from the project publisher you trust. Checksums detect damage; they do not authenticate the publisher.')
    if input('Install this release? [y/N] ').strip().lower()!='y': print('Update canceled.'); return
    update(root,archive)
