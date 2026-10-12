#!/usr/bin/env python3
"""Supplemental real PHP account write/persistence qualification on suite-owned installs."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import installer as i
import admin

parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
for mode in ('nginx','external'):
    path=Path(args.root)/mode;cfg=admin.load(path)
    try:
        i.compose(path,'up','-d','--wait')
        for target,relative in [('data','storage'),('custom','custom'),('var','cache'),('public','public')]:
            code="if (file_put_contents('/var/www/html/"+target+"/qualification-upload.txt', 'written by PHP account') === false) exit(1);"
            i.compose(path,'exec','-T','--user','www-data','app','php','-r',code)
            if (path/'data'/relative/'qualification-upload.txt').read_text()!='written by PHP account':
                raise AssertionError('Application write failed: '+relative)
        i.compose(path,'up','-d','--wait','--force-recreate')
        i.validate_runtime(path,cfg)
        for relative in ('storage','custom','cache','public'):
            if (path/'data'/relative/'qualification-upload.txt').read_text()!='written by PHP account':
                raise AssertionError('Application write did not persist: '+relative)
        print('PASS:',mode,'PHP account writes all application mounts; content survives recreation',flush=True)
    finally:
        i.compose(path,'down')
