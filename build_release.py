"""Build a clean, checksummed Windows ZIP from the allowlisted application source."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from updater import APP_ID,allowed_file

PUBLIC_FILES=('.gitignore', 'Collector Status.cmd', 'NOTICE.txt', 'Open Dashboard.cmd', 'README.md', 'Setup LiveDesk.cmd', 'Start Collector.cmd', 'Start LiveDesk.cmd', 'Stop Collector.cmd', 'Stop Dashboard.cmd', 'Stop LiveDesk.cmd', 'Update LiveDesk.cmd', 'VERSION', 'app.py', 'bootstrap.py', 'collector.py', 'config.example.json', 'config.json', 'control.py', 'dashboard.html', 'dashboard.py', 'diagnostics.py', 'help.html', 'history.html', 'preferences.py', 'requirements.txt', 'settings.html', 'settings_api.py', 'startup.py', 'stream_list.proto', 'stream_list_pb2.py', 'stream_list_pb2_grpc.py', 'update_helper.py', 'updater.py')


def build(root,destination):
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    if config.get('channels')!=[]: raise ValueError('Release defaults must have an empty channel list.')
    payload={name:(root/name).read_bytes() for name in PUBLIC_FILES}
    if not all(allowed_file(name) for name in payload): raise ValueError('Invalid release file list.')
    version=(root/'VERSION').read_text().strip()
    manifest={'app':APP_ID,'version':version,'files':{n:hashlib.sha256(b).hexdigest() for n,b in sorted(payload.items())}}
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for name,body in sorted(payload.items()): z.writestr('LiveDesk/'+name,body)
        z.writestr('LiveDesk/release.json',json.dumps(manifest,indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('destination',type=Path)
    args=parser.parse_args(); result=build(Path(__file__).resolve().parent,args.destination)
    print('Built LiveDesk '+result['version']+' with '+str(len(result['files']))+' application files.')
