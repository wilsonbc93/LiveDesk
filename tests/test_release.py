from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import bootstrap
import build_release
import diagnostics
import startup
import updater


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.source=self.base/'source'; self.source.mkdir()
        origin=Path(__file__).resolve().parent.parent
        for name in (*build_release.PUBLIC_FILES,'build_release.py'):
            path=origin/name
            target=self.source/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,target)
        config=json.loads((self.source/'config.json').read_text()); config['channels']=[]
        (self.source/'config.json').write_text(json.dumps(config))
        self.archive=self.base/'release.zip'
        build_release.build(self.source,self.archive)

    def tearDown(self): self.temp.cleanup()

    def test_release_is_clean_empty_and_valid(self):
        sentinel=str(self.base)+'private-content'
        for name in ['settings.sqlite3','collector.log','.env','diagnostics.zip','private-notes.txt']:
            (self.source/name).write_text(sentinel)
        private=self.source/'backups'/'code.py'; private.parent.mkdir(); private.write_text(sentinel)
        build_release.build(self.source,self.archive)
        manifest,payload=updater.validate_release(self.archive)
        self.assertEqual(manifest['app'],'livedesk-windows')
        self.assertEqual(json.loads(payload['config.json'])['channels'],[])
        self.assertFalse(any(sentinel.encode() in body for body in payload.values()))
        self.assertIn('Setup LiveDesk.cmd',payload)
        self.assertIn('Start LiveDesk.cmd',payload)

    def test_release_builder_rejects_personal_channel_list(self):
        (self.source/'config.json').write_text(json.dumps({'channels':['@personal']}))
        with self.assertRaises(ValueError): build_release.build(self.source,self.archive)

    def test_corrupt_files_unsafe_names_and_extra_files_rejected(self):
        with zipfile.ZipFile(self.archive) as z: entries={i.filename:z.read(i) for i in z.infolist()}
        entries['LiveDesk/app.py']=b'corrupted'
        bad=self.base/'corrupt.zip'
        with zipfile.ZipFile(bad,'w') as z:
            for name,body in entries.items(): z.writestr(name,body)
        with self.assertRaises(ValueError): updater.validate_release(bad)
        for filename in ['../outside.py','LiveDesk/APP.py','LiveDesk/extra.py']:
            shutil.copyfile(self.archive,bad)
            with zipfile.ZipFile(bad,'a') as z: z.writestr(filename,b'x')
            with self.assertRaises(ValueError): updater.validate_release(bad)
        for name in ['CON.py','foo.py.','AUX.txt','../app.py','settings.sqlite3','backups/code.py']:
            self.assertFalse(updater.allowed_file(name),name)

    def test_install_and_rollback_preserve_configuration_and_history(self):
        root=self.base/'installed'; root.mkdir()
        config=b'{"channels":["@mine"],"database":"superchats.sqlite3"}'
        (root/'config.json').write_bytes(config); (root/'app.py').write_bytes(b'original code')
        (root/'settings.sqlite3').write_bytes(b'private data')
        manifest,payload=updater.validate_release(self.archive)
        saved=updater.apply_payload(root,manifest,payload,root/'backups'/'test')
        self.assertEqual((root/'config.json').read_bytes(),config)
        self.assertEqual((root/'settings.sqlite3').read_bytes(),b'private data')
        updater.rollback_payload(root,saved)
        self.assertEqual((root/'app.py').read_bytes(),b'original code')
        self.assertFalse((root/'history.html').exists())

    def test_environment_failure_rolls_back_code_and_runtime(self):
        root=self.base/'installed'; root.mkdir()
        (root/'config.json').write_text('{"channels":[]}')
        (root/'app.py').write_text('original code')
        (root/'.venv').mkdir(); (root/'.venv'/'original').write_text('runtime')
        with closing(sqlite3.connect(root/'settings.sqlite3')) as db,db:
            db.execute('CREATE TABLE preserved(value TEXT)'); db.execute("INSERT INTO preserved VALUES ('history')")
        def prepare(code,environment):
            environment.mkdir(); (environment/'candidate').write_text('new runtime')
        with patch('control.running',return_value=False),patch('dashboard.available',return_value=False),\
             patch('subprocess.run',return_value=subprocess.CompletedProcess([],0)),\
             patch('bootstrap.verify',side_effect=RuntimeError('bad environment')):
            with self.assertRaisesRegex(RuntimeError,'bad environment'):
                updater.update(root,self.archive,prepare)
        self.assertEqual((root/'app.py').read_text(),'original code')
        self.assertTrue((root/'.venv'/'original').exists())
        with closing(sqlite3.connect(root/'settings.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0],'history')
        self.assertEqual(len(list((root/'backups').glob('*/databases/settings.sqlite3'))),1)

    def test_moves_must_stay_inside_installation(self):
        with self.assertRaises(ValueError): updater.move_inside(self.source,self.source,self.base/'outside')

    def test_diagnostics_redacts_and_excludes_databases(self):
        (self.source/'collector.log').write_text('api_key=secret-value token=other-secret '+str(self.source)+'\n')
        (self.source/'chat-messages.sqlite3').write_text('private chat contents')
        data={'collector_running':True,'options':{'paused':False,'public_check_interval_seconds':60},
              'streamers':[],'storage':[{'kind':'chat','messages':1,'bytes':100}]}
        with patch('settings_api.settings_data',return_value=data),patch('startup.enabled',return_value=False):
            content=diagnostics.bundle(self.source)
        self.assertNotIn(str(self.source).replace('\\','/'),diagnostics.redact(str(self.source).replace('\\','/'),self.source))
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            self.assertEqual(set(z.namelist()),{'README.txt','health.json','logs/collector.log'})
            text=b' '.join(z.read(name) for name in z.namelist()).decode()
            for private in ['secret-value','other-secret','private chat contents',str(self.source)]:
                self.assertNotIn(private,text)

    def test_login_shortcut_scoped_and_no_execution_policy_bypass(self):
        root=self.base/"folder with spaces and ' quote"; root.mkdir()
        python=root/'.venv/Scripts/pythonw.exe'; python.parent.mkdir(parents=True); python.touch()
        link=root/'startup'/'LiveDesk.lnk'
        with patch('startup.shortcut',return_value=link),patch('subprocess.run') as run:
            startup.set_enabled(root,True)
            command=run.call_args.args[0]
            self.assertIn('-WindowStyle',command); self.assertNotIn('-ExecutionPolicy',command)
            self.assertIn("'' quote",command[-1])
            link.touch(); startup.set_enabled(root,False); self.assertFalse(link.exists())

    def test_setup_rejects_unsupported_python(self):
        with patch.object(bootstrap.sys,'version_info',(3,10)):
            with self.assertRaisesRegex(RuntimeError,'3.11'): bootstrap.prepare(self.source)


if __name__=='__main__': unittest.main()
