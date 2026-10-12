#!/usr/bin/env python3
"""Real Docker smoke test. No image builds, test registry or application hooks."""
import json
import pty
import select
import time
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
    args=[str(standalone),'--install-dir',str(path),'--web-mode',mode,'--http-bind','127.0.0.1',
          '--http-port' if mode=='nginx' else '--fpm-port',str(port)]
    result=subprocess.run(args,text=True,capture_output=True)
    print(result.stdout,end='')
    if ok and result.returncode:raise AssertionError(result.stderr)
    if not ok:check(result.returncode!=0,'Expected installer rejection')

def free_port():
    with socket.socket() as stream:
        stream.bind(('0.0.0.0',0));return stream.getsockname()[1]


def terminal(argv,answers):
    master,slave=pty.openpty()
    process=subprocess.Popen(argv,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
    os.close(slave);os.write(master,answers.encode());output=b'';deadline=time.monotonic()+300
    try:
        while time.monotonic()<deadline:
            ready,_,_=select.select([master],[],[],1)
            if ready:
                try:output+=os.read(master,65536)
                except OSError:break
            if process.poll() is not None and not ready:break
        else:process.kill();raise AssertionError('Interactive command timed out')
    finally:os.close(master)
    check(process.wait(timeout=10)==0,output.decode(errors='replace'))
    return output.decode(errors='replace')


def own_certificate(directory,name='localhost'):
    directory.mkdir(mode=0o700,exist_ok=True)
    cert=directory/'certificate.pem';key=directory/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','7','-subj','/CN='+name,
                    '-addext','subjectAltName=DNS:'+name,'-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    key.chmod(0o600);return cert,key


def container_logs(name):
    return subprocess.run(i.docker_command('logs','--tail','100',name),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True,env=i.docker_environment()).stdout


def snapshot():
    ids=i.run(['docker','ps','-aq']).split()
    return {v['Id']:(v['State']['Status'],v['State']['StartedAt'],v['RestartCount']) for v in json.loads(i.run(['docker','inspect',*ids]))} if ids else {}

root.mkdir(mode=0o700,exist_ok=False)
# Copy only the entry script: prove that its embedded implementation is self-contained.
standalone=root/'install.sh';shutil.copyfile(repo/'install.sh',standalone);standalone.chmod(0o755)
# A sentinel complements the existing-host snapshot, proving scoped lifecycle operations.
sentinel='installer-smoke-sentinel-'+uuid.uuid4().hex[:8]
i.run(['docker','run','-d','--name',sentinel,'redis:8-alpine'])
before=snapshot();paths=[];pebble=None
try:
    if '--web-only' not in sys.argv:
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
            if mode=='nginx':
                # User-editable PHP/FPM configuration, validated before recreation.
                php_ini=path/'config/php.ini'
                php_ini.write_text(php_ini.read_text()+'\nmemory_limit = 192M\n')
                command(path,'apply-config')
                check(i.compose(path,'exec','-T','php','php','-r','echo ini_get("memory_limit");')=='192M','PHP configuration was not applied')
                nginx_text=(path/'config/nginx.conf').read_text()
                (path/'config/nginx.conf').write_text('invalid nginx directive;\n')
                command(path,'apply-config',ok=False)
                check(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','Invalid config changed data')
                (path/'config/nginx.conf').write_text(nginx_text)
                command(path,'apply-domain','localhost')
                sql=root/'database.sql';command(path,'export-sql',str(sql))
                check(sql.stat().st_mode & 0o077==0,'SQL dump permissions')
                command(path,'export-table-sql',str(root/'table.sql'),'smoke')
                command(path,'import-sql',str(sql),ok=False)
                i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-c',"UPDATE smoke SET value='changed';")
                command(path,'import-sql',str(sql),'--confirm',cfg['project'])
                check(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','SQL restore')
                invalid=root/'invalid.sql';invalid.write_text('THIS IS INVALID SQL;')
                command(path,'import-sql',str(invalid),'--confirm',cfg['project'],ok=False)
                check(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','Failed SQL import lost database')
                backup_destination=root/'offsite-backups';command(path,'backup',str(backup_destination))
                portable=max(backup_destination.iterdir(),key=lambda item:item.name)
                restored=root/'restored';paths.append(restored)
                result=subprocess.run([str(standalone),'--restore',str(portable),'--install-dir',str(restored),'--http-port',str(free_port()),'--http-bind','127.0.0.1'],text=True,capture_output=True)
                check(result.returncode==0,result.stdout+result.stderr)
                command(restored,'health')
                check((restored/'.env').read_bytes()==secret,'Fresh restore credentials')
                check(i.compose(restored,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-Atc','SELECT value FROM smoke')=='preserved','Fresh restore database')
                check((restored/'data/public/upload.txt').read_text()=='preserved','Fresh restore files')
                i.compose(restored,'stop')
                cert,key=own_certificate(root/'own-cert');https=free_port()
                command(path,'configure-web','own','localhost',str(cert),str(key),'--https-port',str(https))
                command(path,'health')
                import http.client
                connection=http.client.HTTPConnection('127.0.0.1',i.load(path)['http_port'],timeout=5)
                connection.request('GET','/');response=connection.getresponse()
                check(response.status==301 and response.getheader('Location')==f'https://localhost:{https}/','HTTP did not redirect to HTTPS')
                response.read();connection.close()
                other_cert,other_key=own_certificate(root/'wrong-key')
                command(path,'cert-install','localhost',str(cert),str(other_key),ok=False)
                command(path,'health')
                command(path,'cert-install','localhost',str(other_cert),str(other_key))
                print('PASS standalone packaging, PHP configuration, SQL import/export and rollback, fresh restore, own HTTPS/reload')
            print('PASS '+mode+': health, rerun, persistence, backup/restore, update, collisions and isolation')
            i.compose(path,'stop')
    path=root/'acme';paths.append(path)
    ca,key=own_certificate(root/'acme-server')
    http_port=free_port();https_port=free_port();ca_port=free_port()
    config={'pebble':{'listenAddress':':14000','managementListenAddress':':15000','certificate':'/test/certificate.pem','privateKey':'/test/key.pem','httpPort':http_port,'tlsPort':https_port,'ocspResponderURL':'','externalAccountBindingRequired':False}}
    (root/'acme-server/config.json').write_text(json.dumps(config))
    pebble='installer-smoke-acme-'+uuid.uuid4().hex[:8]
    i.run(['docker','run','-d','--name',pebble,'--add-host','installer.test:host-gateway',
           '-e','PEBBLE_VA_NOSLEEP=1','-e','PEBBLE_WFE_NONCEREJECT=0','-e','PEBBLE_AUTHZREUSE=0',
           '-v',str(root/'acme-server')+':/test:ro','-p',f'127.0.0.1:{ca_port}:14000',
           'ghcr.io/letsencrypt/pebble:2.8.0','-config','/test/config.json','-strict=false'])
    import ssl,urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
    for attempt in range(30):
        try:
            with opener.open(f'https://localhost:{ca_port}/dir',timeout=2) as response:check('newNonce' in json.load(response),'ACME directory is invalid')
            break
        except OSError:
            if attempt==29:raise AssertionError(container_logs(pebble))
            time.sleep(1)
    result=subprocess.run([str(standalone),'--install-dir',str(path),'--web-mode','nginx','--tls-mode','letsencrypt',
                           '--domain','installer.test','--email','operator@example.test','--http-port',str(http_port),
                           '--https-port',str(https_port),'--acme-server',f'https://localhost:{ca_port}/dir','--acme-ca',str(ca),'--no-auto-renew'],text=True,capture_output=True)
    check(result.returncode==0,result.stdout+result.stderr)
    command(path,'health')
    cfg=i.load(path)
    cert=path/'config/letsencrypt/live/installer.test/fullchain.pem'
    previous=cert.read_bytes();command(path,'cert-renew','--force')
    check(cert.read_bytes()!=previous,'Forced ACME renewal did not replace certificate')
    check('/.well-known/acme-challenge/' in i.compose(path,'logs','nginx'),'ACME did not request a real HTTP-01 challenge')
    command(path,'cert-cron-add')
    check(i.compose(path,'ps','-q','certbot'),'Container renewal scheduler not running')
    command(path,'cert-cron-remove');command(path,'health')
    command(path,'backup');saved=max((path/'backups').iterdir(),key=lambda item:item.name)
    command(path,'restore',str(saved),'--confirm',cfg['project']);command(path,'health')
    print('PASS actual ACME HTTP-01 issuance, forced renewal, automatic-renewal container and certificate backup/restore')
    interactive=root/'interactive';paths.append(interactive)
    output=terminal([str(standalone),'--install-dir',str(interactive)],f'\n2\nlocalhost\n{free_port()}\ny\n')
    check('Infrastructure healthy' in output,'Interactive installer did not complete')
    check('Healthy' in terminal([str(interactive/'command.sh')],'health\n'),'Interactive administration menu')
    subprocess.run([str(standalone),'--command','--install-dir',str(interactive)],check=True)
    print('PASS real terminal installation, administration menu and tooling-only update')
    after=snapshot()
    for cid,state in before.items():check(after.get(cid)==state,'Unrelated container changed: '+cid)
    print('PASS all existing containers unchanged')
finally:
    for path in paths:
        if (path/'compose.yaml').exists():i.compose(path,'down')
    if pebble:
        print('ACME server diagnostics: '+container_logs(pebble))
        i.run(['docker','rm','-f',pebble])
    i.run(['docker','rm','-f',sentinel])
    print('Suite-owned containers removed; persistent test data retained at '+str(root))
