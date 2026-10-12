#!/usr/bin/env python3
"""Real daemon/config/backup/restore isolation under hostile exported variables."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import installer as i
import admin
parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
for mode in ('nginx','external'):
    path=Path(args.root)/mode;cfg=admin.load(path)
    # Refresh only the suite-owned installed control scripts for this focused regression.
    for name in ('installer.py','admin.py','fastcgi.py'):
        (path/'src'/name).write_text((i.REPO/'src'/name).read_text())
    original=dict(os.environ)
    def cli(*arguments):
        result=subprocess.run([str(path/'command.sh'),*arguments],capture_output=True,text=True)
        if result.returncode:raise AssertionError(result.stdout+result.stderr)
        return result.stdout.strip()
    try:
        os.environ.update({'COMPOSE_PROJECT_NAME':'installer-ambient-scope-test','APP_IMAGE':'injected:latest','APP_SECRET':'injected','POSTGRES_PASSWORD':'injected','HTTP_PORT':'19998','FPM_PORT':'19999','DOCKER_HOST':'unix:///tmp/installer-nonexistent.sock','DOCKER_CONTEXT':'nonexistent','COMPOSE_PROFILES':'unexpected'})
        rendered=json.loads(i.compose(path,'config','--format','json'))
        assert rendered['name']==cfg['project'] and rendered['services']['app']['image']==cfg['images']['app']
        cli('start');cli('health')
        backup=Path(cli('backup').splitlines()[-1])
        i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-c',"UPDATE persistence SET value='ambient-change'")
        cli('restore',str(backup),'--confirm',cfg['project'])
        value=i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-At','-c','SELECT value FROM persistence')
        assert value=='preserved'
        print('PASS:',mode,'saved project, image, secrets, ports and local daemon override hostile environment; backup/restore preserve data',flush=True)
    finally:
        i.compose(path,'down');os.environ.clear();os.environ.update(original)
