#!/usr/bin/env python3
"""Real Docker acceptance suite. Run as root on a disposable or shared supported host.
Only this suite's projects and named auxiliary containers are removed; data retained.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import signal
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import installer as i
import admin


def check(value, message):
    if not value: raise AssertionError(message)
    print('PASS:',message,flush=True)


def cli(path,*args,ok=True):
    result=subprocess.run([str(path/'command.sh'),*args],capture_output=True,text=True)
    if ok and result.returncode: raise AssertionError(result.stdout+result.stderr)
    if not ok: check(result.returncode!=0,'expected operation failure '+args[0])
    return result.stdout.strip()


def sql(path, query):
    return i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-At','-v','ON_ERROR_STOP=1','-c',query)


def install(path,image,mode,port):
    flags=[]
    if mode=='nginx':
        flags=['--tls-cert',str(path.parent/'cert.pem'),'--tls-key',str(path.parent/'key.pem'),'--https-port','18443']
    result=subprocess.run([str(i.REPO/'install.sh'),'--install-dir',str(path),'--web-mode',mode,'--http-bind','127.0.0.1','--http-port',str(port),'--fpm-port',str(port),'--app-image',image,*flags],capture_output=True,text=True)
    if result.returncode: raise AssertionError(result.stdout+result.stderr)
    print(result.stdout,flush=True)



def web_security(path, urls):
    (path/'data/public/forbidden.php').write_text('<?php echo "sensitive";')
    (path/'data/public/.private').mkdir(exist_ok=True)
    (path/'data/public/.private/config').write_text('sensitive')
    for url in urls:
        for endpoint in ('/forbidden.php','/.private/config'):
            try:
                urllib.request.urlopen(url+endpoint)
            except urllib.error.HTTPError as exc:
                check(exc.code in (403,404), 'web server denies '+endpoint+' on '+url)
            else:
                raise AssertionError('Sensitive endpoint exposed: '+url+endpoint)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);parser.add_argument('--bad-image',required=True);parser.add_argument('--next-image',required=True);parser.add_argument('--incompatible-image',required=True);parser.add_argument('--unhealthy-image',required=True);parser.add_argument('--root',required=True);args=parser.parse_args()
    root=Path(args.root);root.mkdir(mode=0o755,parents=True,exist_ok=False)
    before=[json.loads(line) for line in i.run(['docker','ps','-a','--format','json']).splitlines()]
    (root/'containers-before.json').write_text(json.dumps(before,indent=2))
    i.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem')])
    projects=[]; auxiliary=[]
    try:
        interrupted=root/'interrupted';projects.append(interrupted)
        with (root/'interruption.log').open('w') as output:
            process=subprocess.Popen([str(i.REPO/'install.sh'),'--install-dir',str(interrupted),'--web-mode','nginx','--http-bind','127.0.0.1','--http-port','18084','--app-image',args.image],stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            injected=False
            while time.monotonic()<deadline and process.poll() is None:
                if (interrupted/'integrity.json').exists():
                    cid=i.compose(interrupted,'ps','--status','running','-q','db')
                    if cid and not (interrupted/'installed.json').exists():
                        os.killpg(process.pid,signal.SIGKILL);process.wait();injected=True;break
                time.sleep(0.2)
            if not injected:
                if process.poll() is None: os.killpg(process.pid,signal.SIGKILL);process.wait()
                raise AssertionError('Failed to inject real provisioning interruption')
        secret=(interrupted/'.env').read_bytes()
        install(interrupted,args.image,'nginx',18084)
        check((interrupted/'.env').read_bytes()==secret,'real killed installer resumes without replacing credentials')
        i.compose(interrupted,'down')
        for mode, port in [('nginx',18081),('external',19081)]:
            path=root/mode
            with socket.socket() as sock:
                sock.bind(('0.0.0.0',port));sock.listen()
                result=subprocess.run([str(i.REPO/'install.sh'),'--install-dir',str(path),'--web-mode',mode,'--http-port',str(port),'--fpm-port',str(port),'--app-image',args.image],capture_output=True)
                check(result.returncode!=0 and not path.exists(),'port collision fails before installation')
            projects.append(path)
            install(path,args.image,mode,port)
            cfg=admin.load(path); credentials=(path/'.env').read_bytes()
            sql(path,'CREATE TABLE persistence (value text); INSERT INTO persistence VALUES (\'preserved\');')
            (path/'data/storage/sentinel').write_text('preserved')
            i.compose(path,'exec','-T','redis','redis-cli','SET','sentinel','preserved')
            cli(path,'stop'); install(path,args.image,mode,port)
            check((path/'.env').read_bytes()==credentials,'rerun preserves credentials')
            cli(path,'health')
            cli(path,'restart')
            check('app' in cli(path,'status'),'status reports application')
            check(json.loads(cli(path,'version'))['images']['app']==args.image,'version reports pinned image')
            cli(path,'logs','app')
            (path/'installed.json').unlink();install(path,args.image,mode,port)
            check((path/'installed.json').exists(),'interrupted installation resumes')
            i.compose(path,'up','-d','--wait','--force-recreate')
            check(sql(path,'SELECT value FROM persistence')=='preserved','database survives recreation')
            check((path/'data/storage/sentinel').read_text()=='preserved','files survive recreation')
            check(i.compose(path,'exec','-T','redis','redis-cli','GET','sentinel')=='preserved','Redis survives recreation')
            if mode=='nginx':
                check(urllib.request.urlopen(f'http://127.0.0.1:{port}/').read()==b'installer fixture','HTTP PHP-FPM works')
                context=ssl.create_default_context(cafile=str(root/'cert.pem'))
                check(urllib.request.urlopen('https://127.0.0.1:18443/health',context=context).status==200,'HTTPS certificate and PHP health work')
                cli(path,'renew-tls',str(root/'cert.pem'),str(root/'key.pem'))
                check(urllib.request.urlopen('https://127.0.0.1:18443/health',context=context).status==200,'TLS certificate reload works')
            else:
                # Operator vhost exercised by an isolated container sharing host networking.
                name='installer-v2-external-proxy-'+str(os.getpid());auxiliary.append(name)
                conf=path/'config/operator-test.conf'
                conf.write_text((path/'config/nginx.conf.example').read_text().replace('listen 80;', 'listen 18082;'))
                i.run(['docker','run','-d','--name',name,'--network','host','-v',str(conf)+':/etc/nginx/conf.d/default.conf:ro','-v',str(path/'data/public')+':'+str(path/'data/public')+':ro',cfg['images']['nginx']])
                time.sleep(2)
                check(urllib.request.urlopen('http://127.0.0.1:18082/').read()==b'installer fixture','external operator Nginx FastCGI vhost works')
                apache='installer-v2-external-apache-'+str(os.getpid());auxiliary.append(apache)
                apache_image=i.image_digest('httpd:2.4-alpine')
                default=i.run(['docker','run','--rm','--entrypoint','cat',apache_image,'/usr/local/apache2/conf/httpd.conf'])
                default=default.replace('Listen 80','Listen 18083').replace('#LoadModule proxy_module','LoadModule proxy_module').replace('#LoadModule proxy_fcgi_module','LoadModule proxy_fcgi_module').replace('#LoadModule rewrite_module','LoadModule rewrite_module')
                apache_conf=path/'config/apache-test.conf'
                apache_conf.write_text(default+'\nServerName localhost\n'+(path/'config/apache.conf.example').read_text().replace('*:80','*:18083'))
                i.run(['docker','run','-d','--name',apache,'--network','host','-v',str(apache_conf)+':/usr/local/apache2/conf/httpd.conf:ro','-v',str(path/'data/public')+':'+str(path/'data/public')+':ro',apache_image])
                time.sleep(2)
                check(urllib.request.urlopen('http://127.0.0.1:18083/').read()==b'installer fixture','external operator Apache FastCGI vhost works')
                check(urllib.request.urlopen('http://127.0.0.1:18083/health').status==200,'Apache health vhost works')
            web_security(path,[f'http://127.0.0.1:{port}'] if mode=='nginx' else ['http://127.0.0.1:18082','http://127.0.0.1:18083'])
            i.run(['mount','-t','tmpfs','-o','size=32m','tmpfs',str(path/'backups')])
            try:
                cli(path,'backup',ok=False)
                check(sql(path,'SELECT value FROM persistence')=='preserved','insufficient backup disk preserves database')
                cli(path,'health')
                i.run(['mount','-o','remount,ro,size=512m',str(path/'backups')])
                cli(path,'backup',ok=False)
                check(sql(path,'SELECT value FROM persistence')=='preserved','read-only backup filesystem preserves database')
                cli(path,'health')
            finally:
                i.run(['umount',str(path/'backups')])
            cli(path,'upgrade',args.incompatible_image,'--confirm',cfg['project'],ok=False)
            check(not (path/'upgrade.json').exists() and sql(path,'SELECT value FROM persistence')=='preserved','incompatible upgrade rejected before mutation')
            backup=Path(cli(path,'backup').splitlines()[-1]);admin.verify(backup)
            sql(path,"UPDATE persistence SET value='changed'")
            (path/'data/storage/sentinel').write_text('changed')
            cli(path,'restore',str(backup),ok=False)
            cli(path,'restore',str(backup),'--confirm',cfg['project'])
            check(sql(path,'SELECT value FROM persistence')=='preserved','database backup restores')
            check((path/'data/storage/sentinel').read_text()=='preserved','file backup restores')
            cli(path,'upgrade',args.bad_image,'--confirm',cfg['project'],ok=False)
            check(sql(path,'SELECT value FROM persistence')=='preserved','failed migration rollback restores data')
            check(sql(path,"SELECT count(*) FROM pg_namespace WHERE nspname='failed_upgrade'")=='0','rollback removes migration-created schemas')
            cli(path,'upgrade',args.unhealthy_image,'--confirm',cfg['project'],ok=False)
            check(sql(path,'SELECT value FROM persistence')=='preserved','failed post-upgrade health restores database')
            check(admin.load(path)['images']['app']==args.image,'failed upgrade restores image')
            cli(path,'upgrade',args.next_image,'--confirm',cfg['project'])
            check((path/'data/public/version.txt').read_text().strip()=='2','successful upgrade replaces application assets')
            check(sql(path,"SELECT count(*) FROM migration_probe")=='0','successful migration upgrade')
            # Simulate process death after migration by retaining a pre-upgrade backup journal.
            saved=admin.backup(path,admin.load(path));i.safe_json(path/'upgrade.json',{'backup':str(saved)})
            sql(path,'DELETE FROM persistence')
            cli(path,'health',ok=False);cli(path,'recover','--confirm',cfg['project'])
            check(sql(path,'SELECT value FROM persistence')=='preserved','interrupted migration recovery')
            saved=admin.backup(path,admin.load(path));i.safe_json(path/'restore.json',{'backup':str(saved)})
            sql(path,'DELETE FROM persistence');shutil.rmtree(path/'data/public');(path/'settings.json').write_text('{')
            cli(path,'recover','--confirm',cfg['project'])
            check(sql(path,'SELECT value FROM persistence')=='preserved','interrupted restore recovers corrupt partial configuration')
            i.compose(path,'stop','db');cli(path,'health',ok=False);cli(path,'start')
            check(sql(path,'SELECT value FROM persistence')=='preserved','database outage recovery preserves data')
            corrupt=backup/'database.dump'; original=corrupt.read_bytes();corrupt.write_bytes(b'corrupted')
            cli(path,'restore',str(backup),'--confirm',cfg['project'],ok=False);corrupt.write_bytes(original)
            check(sql(path,'SELECT value FROM persistence')=='preserved','corrupt backup rejected without data loss')
            ambient=dict(os.environ)
            try:
                os.environ.update({'COMPOSE_PROJECT_NAME':'installer-ambient-scope-test','APP_IMAGE':'injected:latest','POSTGRES_PASSWORD':'injected','APP_SECRET':'injected','HTTP_PORT':'19998','FPM_PORT':'19999','DOCKER_HOST':'unix:///tmp/installer-nonexistent.sock','DOCKER_CONTEXT':'nonexistent','COMPOSE_PROFILES':'unexpected'})
                rendered=json.loads(i.compose(path,'config','--format','json'))
                check(rendered['name']==cfg['project'] and rendered['services']['app']['image']==admin.load(path)['images']['app'],'ambient variables cannot change project or pinned image')
                cli(path,'stop');install(path,args.image,mode,port);cli(path,'health')
                saved=Path(cli(path,'backup').splitlines()[-1])
                sql(path,"UPDATE persistence SET value='ambient-change'")
                cli(path,'restore',str(saved),'--confirm',cfg['project'])
                check(sql(path,'SELECT value FROM persistence')=='preserved','backup/restore ignores ambient credentials and Docker context')
                cli(path,'upgrade',args.next_image,'--confirm',cfg['project'])
                check(sql(path,'SELECT value FROM persistence')=='preserved','upgrade ignores ambient project/image/context overrides')
            finally:
                os.environ.clear();os.environ.update(ambient)
            check(i.run(['docker','network','inspect',cfg['project']+'_backend','--format','{{.Internal}}'])=='true','database/Redis backend network is internal')
            for service in ('db','redis'):
                cid=i.compose(path,'ps','-q',service)
                check(i.run(['docker','inspect',cid,'--format','{{json .HostConfig.PortBindings}}']) in ('{}','null'),'private '+service+' ports')
            if mode=='external':
                cid=i.compose(path,'ps','-q','app')
                check('127.0.0.1' in i.run(['docker','inspect',cid,'--format','{{json .HostConfig.PortBindings}}']),'FastCGI loopback only')
        for container in before:
            now=json.loads(i.run(['docker','inspect',container['ID']]))[0]
            check(now['State']['Status']==container['State'],'unrelated container unchanged '+container['Names'])
        print('ALL REAL DOCKER ACCEPTANCE CHECKS PASSED',flush=True)
    finally:
        for name in auxiliary: i.run(['docker','rm','-f',name])
        for path in projects:
            if (path/'compose.yaml').exists():
                logs=i.compose(path,'logs','--tail=100')
                (root/(path.name+'-services.log')).write_text(logs)
                evidence=i.REPO/'evidence';evidence.mkdir(exist_ok=True)
                (evidence/(path.name+'-services.log')).write_text(logs)
                i.compose(path,'down')
        print('Test installation data retained at',root,flush=True)

if __name__=='__main__': main()
