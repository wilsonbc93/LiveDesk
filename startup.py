"""Optional per-user login shortcut, scoped to this installation."""
import hashlib
import os
from pathlib import Path
import subprocess


def shortcut(root):
    tag=hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:12]
    return Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'/('LiveDesk-'+tag+'.lnk')


def enabled(root):
    return shortcut(root).is_file() if os.name=='nt' else False


def set_enabled(root,value):
    if type(value) is not bool: raise ValueError('Invalid login preference.')
    target=shortcut(root)
    if not value:
        target.unlink(missing_ok=True); return
    python=root/'.venv/Scripts/pythonw.exe'
    if not python.exists(): raise ValueError('Run Setup LiveDesk.cmd before enabling login startup.')
    target.parent.mkdir(parents=True,exist_ok=True)
    def ps(text): return "'"+str(text).replace("'","''")+"'"
    command=("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("+ps(target)+
        ');$s.TargetPath='+ps(python)+';$s.Arguments='+ps('"'+str(root/'app.py')+'" start --no-browser')+
        ';$s.WorkingDirectory='+ps(root)+';$s.Description='+ps('Start this LiveDesk installation at login')+';$s.Save()')
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-Command',command],
                   check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
