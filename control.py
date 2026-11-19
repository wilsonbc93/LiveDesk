"""Windows start/stop/status controls for the background collector."""
import argparse
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / 'collector.lock'
STOP_FILE = ROOT / 'collector.stop'


def acquire():
    handle = LOCK.open('a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b' '); handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def running():
    lock = acquire()
    if lock is not None:
        lock.close()
        return False
    return True


def run():
    lock = acquire()
    if lock is None:
        return
    with lock:
        lock.seek(1); lock.truncate(); lock.write(str(os.getpid()).encode()); lock.flush()
        STOP_FILE.unlink(missing_ok=True)
        import collector
        from preferences import Preferences
        prefs=Preferences(ROOT)
        finished=threading.Event()
        def maintenance():
            last_cleanup=0; last_policy=None
            while not finished.is_set():
                try:
                    options=prefs.read()['options']
                    policy=(options['chat_retention_days'],options['paid_retention_days'])
                    if policy!=last_policy or time.monotonic()-last_cleanup>=3600:
                        prefs.retain(); last_cleanup=time.monotonic(); last_policy=policy
                except Exception:
                    collector.LOG.exception('Retention cleanup failed; will retry')
                finished.wait(5)
        threading.Thread(target=maintenance,daemon=True).start()
        def monitoring_signature():
            state=prefs.read()
            return (state['options']['paused'],state['options']['public_check_interval_seconds'],
                    tuple(sorted((s['channel_id'],s['name']) for s in state['streamers']
                                 if s['enabled'] and (s['record_chat'] or s['record_paid']))))
        try:
            while not STOP_FILE.exists():
                state=prefs.read()['options']
                if state['paused']:
                    time.sleep(0.5); continue
                signature=monitoring_signature()
                collector.STOP.clear(); collector.WAKE.clear()
                reload_requested=threading.Event()
                def monitor():
                    while not collector.STOP.wait(0.5):
                        if STOP_FILE.exists() or monitoring_signature()!=signature:
                            if not STOP_FILE.exists(): reload_requested.set()
                            collector.STOP.set(); collector.WAKE.set(); return
                watcher=threading.Thread(target=monitor,daemon=True); watcher.start()
                try:
                    sys.argv = [str(ROOT / 'collector.py')]
                    collector.main()
                finally:
                    collector.STOP.set(); collector.WAKE.set(); watcher.join(timeout=2)
                if not reload_requested.is_set(): break
        finally:
            finished.set()
            collector.STOP.set(); collector.WAKE.set()
            STOP_FILE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'run'])
    action = parser.parse_args().action
    if action == 'run':
        run()
    elif action == 'start':
        if running():
            print('Collector is already running.'); return
        STOP_FILE.unlink(missing_ok=True)
        with (ROOT / 'collector.log').open('ab', buffering=0) as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(50):
            if running():
                print('Collector started in the background.'); return
            if process.poll() is not None:
                raise SystemExit('Collector could not start. See collector.log.')
            time.sleep(0.1)
        raise SystemExit('Startup was not confirmed. See collector.log.')
    elif action == 'stop':
        if not running():
            print('Collector is stopped.'); return
        STOP_FILE.touch()
        for _ in range(90):
            if not running():
                print('Collector stopped cleanly.'); return
            time.sleep(1)
        print('Stop requested; a network operation is still finishing. Check Status shortly.')
    else:
        print('Collector is running.' if running() else 'Collector is stopped.')
        from preferences import Preferences
        state=Preferences(ROOT).read()
        print('Collection is paused.' if state['options']['paused'] else 'Collection is enabled.')
        print('Channels: ' + ', '.join(s['name'] for s in state['streamers'] if s['enabled']))
        print('Log: ' + str(ROOT / 'collector.log'))


if __name__ == '__main__':
    main()
