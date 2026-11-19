"""Unified local start/stop controls. No administrator access is required."""
import argparse
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

ROOT=Path(__file__).resolve().parent


def invoke(script,action):
    result=subprocess.run([sys.executable,str(ROOT/script),action],cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=105)
    if result.returncode: raise RuntimeError(result.stdout.strip() or script+' failed.')
    print(result.stdout.strip())


def start(open_browser=True):
    from collector import Store
    from preferences import Preferences
    import json
    config=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
    Store(ROOT/config.get('database','superchats.sqlite3'),ROOT/config.get('chat_database','chat-messages.sqlite3'))
    Preferences(ROOT)
    invoke('control.py','start')
    invoke('dashboard.py','start')
    if open_browser: webbrowser.open('http://127.0.0.1:8765/')


def stop():
    invoke('control.py','stop')
    import control
    if control.running(): raise RuntimeError('Collection is still finishing. Try Stop LiveDesk again shortly.')
    invoke('dashboard.py','stop')
    import dashboard
    for _ in range(40):
        if not dashboard.available(): return
        time.sleep(.25)
    raise RuntimeError('Dashboard is still stopping. Try again shortly.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status','update'])
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='start': start(not args.no_browser)
        elif args.action=='stop': stop()
        elif args.action=='status': invoke('control.py','status')
        else:
            # Run updates outside the environment being replaced, in a visible helper.
            base=getattr(sys,'_base_executable',sys.executable)
            subprocess.Popen([base,str(ROOT/'update_helper.py')],cwd=ROOT,
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform=='win32' else 0)
        return 0
    except Exception as exc:
        from datetime import datetime
        with (ROOT/'launcher.log').open('a',encoding='utf-8') as log:
            log.write(datetime.now().isoformat()+' '+str(exc)+'\n')
        print('LiveDesk: '+str(exc)); return 1


if __name__=='__main__': raise SystemExit(main())
