#!/usr/bin/env python3
"""Real Docker smoke test. No image builds, test registry or application hooks."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import installer as i
import admin as a

root=Path(sys.argv[1] if len(sys.argv)>1 else '/opt/installer-v2-smoke')
repo=Path(__file__).resolve().parents[1]

def check(condition,message):
    if not condition:raise AssertionError(message)

def command(path,*args,ok=True):
    result=subprocess.run([str(path/'command.sh'),*args],text=True,capture_output=True)
    if ok and result.returncode:raise AssertionError(result.stdout+result.stderr)
    if not ok:check(result.returncode!=0,'Expected rejection: '+str(args))
    return result.stdout

def install(path,mode,port,ok=True):
    args=[str(repo/'install.sh'),'--install-dir',str(path),'--web-mode',mode,'--http-bind','127.0.0.1',
          '--http-port' if mode=='nginx' else '--fpm-port',str(port)]
    result=subprocess.run(args,text=True,capture_output=True)
    print(result.stdout,end='')
    if ok and result.returncode:raise AssertionError(result.stderr)
    if not ok:check(result.returncode!=0,'Expected installer rejection')

def snapshot():
    ids=i.run(['docker','ps','-aq']).split()
    return {v['Id']:(v['State']['Status'],v['State']['StartedAt'],v['RestartCount']) for v in json.loads(i.run(['docker','inspect',*ids]))} if ids else {}

root.mkdir(mode=0o700,exist_ok=False)
# A sentinel complements the existing-host snapshot, proving scoped lifecycle operations.
sentinel='installer-smoke-sentinel-'+uuid.uuid4().hex[:8]
i.run(['docker','run','-d','--name',sentinel,'redis:8-alpine'])
before=snapshot();paths=[]
try:
    for mode in ('nginx','external'):
        path=root/mode
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        paths.append(path);install(path,mode,port)
        cfg=i.load(path);secret=(path/'.env').read_bytes()
        i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-c',"CREATE TABLE smoke(value text); INSERT INTO smoke VALUES ('preserved');")
        i.compose(path,'exec','-T','redis','redis-cli','SET','smoke','preserved')
        (path/'data/public/upload.txt').write_text('preserved')
        install(path,mode,port);check((path/'.env').read_bytes()==secret,'Credentials changed')
        command(path,'stop');install(path,mode,port)
        i.compose(path,'up','-d','--force-recreate','--wait');command(path,'health')
        check(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','DB persistence')
        check(i.compose(path,'exec','-T','redis','redis-cli','GET','smoke')=='preserved','Redis persistence')
        check((path/'data/public/upload.txt').read_text()=='preserved','File persistence')
        for service in ('db','redis'):
            cid=i.compose(path,'ps','-q',service)
            state=json.loads(i.run(['docker','inspect',cid]))[0]
            check(not any(v for v in state['NetworkSettings']['Ports'].values()),service+' publicly published')
        command(path,'backup')
        saved=max((path/'backups').iterdir(),key=lambda p:p.name)
        command(path,'restore',str(saved),ok=False)
        i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-c',"UPDATE smoke SET value='changed';")
        i.compose(path,'exec','-T','redis','redis-cli','SET','smoke','changed')
        (path/'data/public/upload.txt').write_text('changed')
        command(path,'restore',str(saved),'--confirm',cfg['project'])
        command(path,'health')
        check(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','DB restore')
        check(i.compose(path,'exec','-T','redis','redis-cli','GET','smoke')=='preserved','Redis restore')
        check((path/'data/public/upload.txt').read_text()=='preserved','File restore')
        check((path/'.env').read_bytes()==secret,'Restored credentials changed')
        corrupt=root/(mode+'-corrupt');shutil.copytree(saved,corrupt)
        with (corrupt/'stack.tar').open('ab') as stream:stream.write(b'corrupt')
        command(path,'restore',str(corrupt),'--confirm',cfg['project'],ok=False)
        command(path,'update','postgres','postgres:19-alpine','--confirm',cfg['project'],ok=False)
        if mode=='nginx':
            command(path,'update','php','php:8.4.13-fpm-alpine','--confirm',cfg['project'])
            check(i.load(path)['images']['php']!=cfg['images']['php'],'Versioned update did not change image')
        command(path,'update','php','php:8.4-fpm-alpine','--confirm',cfg['project'])
        command(path,'health')
        check((path/'data/public/upload.txt').read_text()=='preserved','Update persistence')
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0));occupied.listen()
            collision=root/(mode+'-collision');install(collision,mode,occupied.getsockname()[1],ok=False)
            check(not collision.exists(),'Collision created installation')
        # Unrelated files are an error; reruns must not adopt arbitrary directories.
        unknown=root/(mode+'-unknown');unknown.mkdir();(unknown/'keep').write_text('keep')
        install(unknown,mode,port,ok=False);check((unknown/'keep').read_text()=='keep','Unknown data changed')
        with_environment={'COMPOSE_PROJECT_NAME':'unrelated','DOCKER_HOST':'unix:///nonexistent','POSTGRES_PASSWORD':'wrong'}
        original=dict(os.environ)
        try:
            os.environ.update(with_environment);command(path,'restart');install(path,mode,port)
        finally:os.environ.clear();os.environ.update(original)
        print('PASS '+mode+': health, rerun, persistence, backup/restore, update, collisions and isolation')
    after=snapshot()
    for cid,state in before.items():check(after.get(cid)==state,'Unrelated container changed: '+cid)
    print('PASS all existing containers unchanged')
finally:
    for path in paths:
        if (path/'compose.yaml').exists():i.compose(path,'down')
    i.run(['docker','rm','-f',sentinel])
    print('Suite-owned containers removed; persistent test data retained at '+str(root))
