"""One-time setup. Uses only Python's standard library before dependencies exist."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parent
MINIMUM=(3,11)


def logged_run(command,root):
    with (Path(root)/'setup.log').open('a',encoding='utf-8') as log:
        process=subprocess.Popen(command,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                 text=True,encoding='utf-8',errors='replace')
        for line in process.stdout:
            print(line,end='',flush=True); log.write(line); log.flush()
        code=process.wait()
        if code: raise RuntimeError('Setup command failed. See setup.log for details.')


def prepare(root,environment=None,interpreter=None):
    root=Path(root); environment=Path(environment or root/'.venv')
    if sys.version_info<MINIMUM: raise RuntimeError('Python 3.11 or newer is required. Install it from python.org/downloads/windows/.')
    python=environment/'Scripts'/'python.exe'
    if not python.exists():
        print('Creating a private Python environment...',flush=True)
        if interpreter:
            logged_run([str(interpreter),'-m','venv',str(environment)],root)
        else: venv.EnvBuilder(with_pip=True).create(environment)
    print('Installing the dependencies listed in requirements.txt from PyPI...',flush=True)
    logged_run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(root/'requirements.txt')],root)
    verify(root,python)
    return python


def verify(root,python):
    logged_run([str(python),'-c',
        'import sys; assert sys.version_info >= (3,11); import requests,grpc,google.protobuf,yt_dlp; import collector,dashboard,preferences'],
        root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=['setup','update'],default='setup')
    parser.add_argument('--zip',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='update':
            from updater import update_interactive
            update_interactive(ROOT,args.zip)
        else:
            prepare(ROOT)
            (ROOT/'.setup-complete.json').write_text(json.dumps({'python':sys.version.split()[0],
                'requirements_sha256':hashlib.sha256((ROOT/'requirements.txt').read_bytes()).hexdigest()}))
            print('\nSetup complete. Double-click Start LiveDesk.cmd. Add streamers in Settings.')
        return 0
    except (Exception,KeyboardInterrupt) as exc:
        with (ROOT/'setup.log').open('a',encoding='utf-8') as log: log.write(str(exc)+'\n')
        print('\nCould not finish: '+str(exc))
        print('Check your internet connection and write access to this folder. See README.md and setup.log.')
        return 1


if __name__=='__main__': raise SystemExit(main())
