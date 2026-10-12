#!/usr/bin/env python3
"""Small installation-scoped infrastructure administration."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess
import posixpath
import tarfile
import tempfile
import time

import installer as i

COMMANDS=('start','stop','restart','status','health','logs','version','backup','restore','update','upgrade','export-sql','export-table-sql','import-sql','apply-config','configure-web','apply-domain','cert-install','cert-generate','cert-renew','cert-cron-add','cert-cron-remove','self-update','help')
SNAPSHOT=('settings.json','.env','compose.yaml','config','data')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def atomic(path,content):
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    with os.fdopen(fd,'w') as stream:
        stream.write(content);stream.flush();os.fsync(stream.fileno())
    os.replace(name,path)


def backup(path,cfg,destination=None):
    i.validate_data(path)
    size=sum(f.stat().st_size for root in SNAPSHOT for f in (path/root).rglob('*') if f.is_file())
    base=Path(destination) if destination else path/'backups'
    i.validate_install_dir(str(base))
    if any(base.is_relative_to(path/name) for name in ('data','config','src')):raise i.InstallError('Backup destination cannot be inside data/config/source')
    if not base.exists():base.mkdir(mode=0o700,parents=True)
    if shutil.disk_usage(base).free < size+128*1024**2:
        raise i.InstallError('Insufficient disk space for backup')
    target=base/str(time.time_ns());target.mkdir(mode=0o700)
    try:
        # Clean shutdown gives PostgreSQL and Redis consistent on-disk snapshots.
        i.compose(path,'stop','--timeout','60')
        for service in ('db','redis'):
            cid=i.compose(path,'ps','--all','-q',service)
            state=json.loads(i.run(['docker','inspect',cid]))[0]['State']
            if state['Running'] or state['ExitCode']!=0:raise i.InstallError('Backup requires clean shutdown of '+service)
        with tarfile.open(target/'stack.tar','w') as archive:
            for name in SNAPSHOT: archive.add(path/name,arcname=name)
        os.chmod(target/'stack.tar',0o600)
        i.safe_json(target/'manifest.json',{'format':'stack-backup-v1','project':cfg['project'],'architecture':os.uname().machine,'sha256':digest(target/'stack.tar')})
        verify(target,cfg['project'])
        return target
    finally:
        i.compose(path,'up','-d','--wait','--wait-timeout','120')
        i.validate_data(path)


def verify(source,project=None):
    manifest=json.loads((source/'manifest.json').read_text())
    if project is None:project=manifest.get('project')
    if manifest.get('format')!='stack-backup-v1' or manifest.get('project')!=project:
        raise i.InstallError('Backup format or installation identity mismatch')
    if digest(source/'stack.tar')!=manifest.get('sha256'): raise i.InstallError('Backup checksum mismatch')
    with tarfile.open(source/'stack.tar') as archive:
        seen=set()
        for member in archive.getmembers():
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or p.parts[0] not in SNAPSHOT or member.name in seen:
                raise i.InstallError('Unsafe backup path')
            if member.issym():
                target=posixpath.normpath(posixpath.join(posixpath.dirname(member.name),member.linkname))
                if not member.name.startswith('config/letsencrypt/live/') or not target.startswith('config/letsencrypt/archive/') or member.linkname.startswith('/'):
                    raise i.InstallError('Unsafe backup symlink')
            elif not (member.isfile() or member.isdir()):raise i.InstallError('Unsupported backup entry')
            if member.mode & 0o6000:raise i.InstallError('Unsafe backup permissions')
            seen.add(member.name)
        if not set(SNAPSHOT).issubset(seen) or not {'data/public','data/postgres','data/redis','config/nginx.conf'}.issubset(seen):
            raise i.InstallError('Incomplete backup')
        cfg=json.load(archive.extractfile('settings.json'))
        if cfg.get('format')!='infrastructure-v1' or cfg.get('project')!=project:
            raise i.InstallError('Invalid backup settings')
        i.validate_images(cfg['images'])
        import web
        web.validate(cfg)
        if not re.fullmatch(r'POSTGRES_PASSWORD=[a-f0-9]{64}\n',archive.extractfile('.env').read().decode()):
            raise i.InstallError('Invalid backup credentials')
        if archive.extractfile('compose.yaml').read().decode()!=i.render_compose(cfg):
            raise i.InstallError('Backup Compose/settings mismatch')
    return cfg


def restore(path,source,cfg,take_safety=True):
    restored=verify(source,cfg['project'])
    # Extract and validate before touching this installation. Never extract into a live tree.
    with tempfile.TemporaryDirectory(prefix='.restore-',dir=path) as temporary:
        stage=Path(temporary)
        with tarfile.open(source/'stack.tar') as archive: archive.extractall(stage,**({'filter':'fully_trusted'} if hasattr(tarfile,'data_filter') else {}))
        if (stage/'.env').stat().st_mode & 0o077: raise i.InstallError('Backup credentials are not private')
        safety=backup(path,cfg) if take_safety else source
        print('Safety backup: '+str(safety))
        i.compose(path,'stop','--timeout','60')
        try:
            for name in SNAPSHOT:
                target=path/name
                if target.is_symlink(): raise i.InstallError('Unsafe restore target')
                if target.is_dir(): shutil.rmtree(target)
                else: target.unlink()
                shutil.move(str(stage/name),str(target))
            i.compose(path,'up','-d','--wait','--force-recreate','--wait-timeout','120')
            i.validate_runtime(path,restored)
        except Exception:
            raise i.InstallError('Restore did not complete. Data and safety backup remain; inspect logs and restore '+str(safety))


def update(path,cfg,service,image):
    # Updates within the installed major are simple recreation; major migrations are operator work.
    patterns={'php':r'php:8\.4(?:\.\d+)?-fpm-alpine(?:\d+\.\d+)?',
              'postgres':r'postgres:18(?:\.\d+)?-alpine(?:\d+\.\d+)?',
              'redis':r'redis:8(?:\.\d+){0,2}-alpine(?:\d+\.\d+)?',
              'nginx':r'nginx:1\.28(?:\.\d+)?-alpine(?:\d+\.\d+)?'}
    if service not in patterns or not re.fullmatch(patterns[service],image):
        raise i.InstallError('Use an official versioned image in the current supported major/minor family')
    if service=='nginx' and cfg['web_mode']!='nginx': raise i.InstallError('Nginx is not enabled')
    resolved=i.image_digest(image)
    saved=backup(path,cfg);old=dict(cfg['images'])
    cfg['images'][service]=resolved
    try:
        atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n')
        atomic(path/'compose.yaml',i.render_compose(cfg))
        i.compose(path,'up','-d','--wait','--wait-timeout','120')
        i.validate_runtime(path,cfg)
    except Exception as exc:
        cfg['images']=old
        atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n');atomic(path/'compose.yaml',i.render_compose(cfg))
        # Restore the cold data snapshot too: the failed image may have written to its volume.
        restore(path,saved,cfg,take_safety=False)
        raise i.InstallError('Image update failed; previous images and data restored') from exc
    print('Updated '+service+'; backup: '+str(saved))


def restore_new(path,source,args):
    if path.exists():raise i.InstallError('Fresh restore requires a nonexistent destination; use command.sh restore for existing installations')
    cfg=verify(source)
    if json.loads((source/'manifest.json').read_text()).get('architecture')!=os.uname().machine:
        raise i.InstallError('Fresh restore requires a backup from the same CPU architecture; use SQL export/import for cross-architecture moves')
    cfg['project']=i.project_name(path)
    if args.http_port is not None:cfg['http_port']=args.http_port
    if args.https_port is not None:cfg['https_port']=args.https_port
    if args.fpm_port is not None:cfg['fpm_port']=args.fpm_port
    if args.http_bind is not None:cfg['http_bind']=args.http_bind
    import web
    web.validate(cfg)
    ports=[cfg['http_port']] if cfg['web_mode']=='nginx' else [cfg['fpm_port']]
    if cfg.get('tls_mode','http')!='http':ports.append(cfg['https_port'])
    for port in ports:
        i.validate_port(port)
        if not i.is_port_free(port,cfg['http_bind'] if cfg['web_mode']=='nginx' else '127.0.0.1'):
            raise i.InstallError(f'Restore port {port} is occupied; override it on the command line')
    path.mkdir(mode=0o755)
    # Data is copied from a validated archive, never deleted from the source server.
    with tarfile.open(source/'stack.tar') as archive:
        archive.extractall(path,**({'filter':'fully_trusted'} if hasattr(tarfile,'data_filter') else {}))
    for name in ('backups','src'):(path/name).mkdir(mode=0o700)
    atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n');atomic(path/'compose.yaml',i.render_compose(cfg))
    i.refresh_tools(path)
    for name in ('nginx.conf.example','apache.conf.example'):
        atomic(path/'config'/name,(i.REPO/'examples'/name).read_text().replace('/opt/fluidweb',str(path)).replace(':9080',':'+str(cfg['fpm_port'])).replace('example.com',cfg.get('domain','example.com')))
    for image in (*cfg['images'].values(),*([cfg['certbot_image']] if cfg.get('certbot_image') else [])):
        i.run(['docker','pull',image],timeout=480,capture=False)
    i.compose(path,'up','-d','--wait','--wait-timeout','120');i.validate_runtime(path,cfg)
    print('Backup restored with preserved credentials/data into '+str(path))


def export_sql(path,target,table=None):
    target=Path(target)
    i.validate_install_dir(str(target))
    if table and not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.]*',table):raise i.InstallError('Use an ordinary table or schema.table identifier')
    arguments=['pg_dump','-U','fluidweb','-d','fluidweb','--clean','--if-exists','--no-owner']
    if table:arguments+=['--table',table]
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as stream:
        subprocess.run(i.compose_command(path,'exec','-T','db',*arguments),stdout=stream,check=True,timeout=300,env=i.compose_environment(path))
    print('SQL export: '+str(target))


def import_sql(path,cfg,source):
    source=Path(source)
    if not source.is_file():raise i.InstallError('SQL file does not exist')
    saved=backup(path,cfg)
    try:
        i.compose(path,'stop','php',*(['nginx'] if cfg['web_mode']=='nginx' else []))
        i.compose(path,'exec','-T','db','dropdb','-U','fluidweb','--force','fluidweb')
        i.compose(path,'exec','-T','db','createdb','-U','fluidweb','-O','fluidweb','fluidweb')
        with source.open('rb') as stream:
            subprocess.run(i.compose_command(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','--single-transaction','--set','ON_ERROR_STOP=1'),stdin=stream,check=True,timeout=300,env=i.compose_environment(path))
        i.compose(path,'up','-d','--wait');i.validate_runtime(path,cfg)
    except Exception as exc:
        restore(path,saved,cfg,take_safety=False)
        raise i.InstallError('SQL import failed; previous database and data restored') from exc
    print('SQL imported; safety backup: '+str(saved))


def apply_config(path,cfg):
    import web
    web.initialize(path,cfg)
    cfg['config_files']=True
    # Validate proposed configuration before stopping services or recreating containers.
    mounts=['-v',str(path/'config/php.ini')+':/usr/local/etc/php/conf.d/zz-installer.ini:ro',
            '-v',str(path/'config/php-fpm.conf')+':/usr/local/etc/php-fpm.d/zz-installer.conf:ro']
    i.run(['docker','run','--rm','--network','none',*mounts,cfg['images']['php'],'php-fpm','-t'])
    if cfg['web_mode']=='nginx':
        extra=[]
        if cfg.get('tls_mode')=='own':extra=['-v',str(path/'config/tls')+':/etc/nginx/tls:ro']
        if cfg.get('tls_mode')=='letsencrypt':extra=['-v',str(path/'config/letsencrypt')+':/etc/letsencrypt:ro','-v',str(path/'config/acme')+':/var/www/certbot:ro']
        i.run(['docker','run','--rm','--network',cfg['project']+'_frontend','-v',str(path/'config/nginx.conf')+':/etc/nginx/conf.d/default.conf:ro',*extra,cfg['images']['nginx'],'nginx','-t'])
    saved=backup(path,i.load(path))
    try:
        atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n');atomic(path/'compose.yaml',i.render_compose(cfg))
        i.compose(path,'up','-d','--wait','--force-recreate');i.validate_runtime(path,cfg);web.save_good(path)
    except Exception:
        for name in ('nginx.conf','php.ini','php-fpm.conf'):
            previous=path/'config/.last-good'/name
            if previous.exists():atomic(path/'config'/name,previous.read_text())
        i.compose(path,'up','-d','--wait','--force-recreate')
        raise i.InstallError('Configuration failed; last applied configuration restored. Safety backup: '+str(saved))


def upgrade(path,cfg):
    resolved={key:i.image_digest(image) for key,image in i.DEFAULT_IMAGES.items()}
    import web
    certbot=i.image_digest(web.CERTBOT_IMAGE) if cfg.get('tls_mode')=='letsencrypt' else None
    saved=backup(path,cfg);old=dict(cfg['images']);cfg['images']=resolved
    if certbot:cfg['certbot_image']=certbot
    try:
        atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n');atomic(path/'compose.yaml',i.render_compose(cfg))
        i.compose(path,'up','-d','--wait');i.validate_runtime(path,cfg)
    except Exception as exc:
        cfg['images']=old
        restore(path,saved,cfg,take_safety=False)
        raise i.InstallError('Upgrade failed; previous infrastructure and data restored') from exc
    print('Infrastructure upgraded; backup: '+str(saved))


def configure_web(path,cfg,values,args):
    import web
    if cfg['web_mode']!='nginx':raise i.InstallError('External HTTPS is managed by your own web-server operator')
    if not values:
        if not sys.stdin.isatty():raise i.InstallError('configure-web requires MODE DOMAIN [EMAIL or CERT KEY]')
        mode=i.ask('Connection: 1=HTTP, 2=Let\'s Encrypt, 3=own certificate','1')
        if mode not in ('1','2','3'):raise i.InstallError('Choose 1, 2 or 3')
        selected={'1':'http','2':'letsencrypt','3':'own'}[mode]
        values=[selected,i.ask('Domain or IP',cfg.get('domain','localhost'))]
        if selected=='letsencrypt':values.append(i.ask('Email',cfg.get('email','')))
        elif selected=='own':values.extend([i.ask('Certificate PEM path'),i.ask('Private key PEM path')])
        args.http_port=int(i.ask('HTTP port',str(cfg['http_port'])))
        if selected!='http':args.https_port=int(i.ask('HTTPS port',str(cfg.get('https_port',443))))
    if len(values)<2 or values[0] not in ('http','own','letsencrypt'):raise i.InstallError('Use http, own or letsencrypt followed by domain and certificate/email arguments')
    mode=values[0];expected={'http':2,'letsencrypt':3,'own':4}[mode]
    if len(values)!=expected:raise i.InstallError('Incorrect arguments for '+mode)
    cfg.update(tls_mode=mode,domain=web.domain(values[1],mode=='letsencrypt'),https_port=args.https_port or cfg.get('https_port',443),auto_renew=mode=='letsencrypt')
    if args.http_port is not None:cfg['http_port']=args.http_port
    if mode=='letsencrypt':cfg['email']=values[2]
    web.apply(path,cfg,*values[2:] if mode=='own' else (None,None))


def self_update(path):
    import urllib.request
    url='https://raw.githubusercontent.com/cristake007/installer-v2/feat/installer-core/install.sh'
    with tempfile.TemporaryDirectory(prefix='installer-update-') as tmp:
        target=Path(tmp)/'install.sh'
        with urllib.request.urlopen(url,timeout=30) as response:target.write_bytes(response.read())
        subprocess.run(['bash',str(target),'--command','--install-dir',str(path)],check=True)


def approval(args,project):
    if args.confirm==project:return
    if sys.stdin.isatty() and i.ask('This replaces data or images. A safety backup will be created. Continue? y/N','N').lower()=='y':return
    raise i.InstallError('Explicit approval required: --confirm '+project)


def menu(args):
    print('Infrastructure administration')
    for index,action in enumerate(COMMANDS,1):print(f'  {index}. {action}')
    choice=i.ask('Command')
    args.action=COMMANDS[int(choice)-1] if choice.isdigit() and 1<=int(choice)<=len(COMMANDS) else choice
    if args.action not in COMMANDS:raise i.InstallError('Unknown command')
    prompts={'backup':['Destination directory (empty=local backups)'],'restore':['Backup directory'],
             'update':['Service (php/postgres/redis/nginx)','Official versioned image'],
             'export-sql':['SQL output file'],'export-table-sql':['SQL output file','Table'],
             'import-sql':['SQL input file'],'apply-domain':['Domain or IP'],
             'cert-install':['Domain or IP','Certificate PEM path','Private key PEM path']}
    args.arguments=[value for prompt in prompts.get(args.action,[]) if (value:=i.ask(prompt))]


def main():
    parser=argparse.ArgumentParser(description='Manage this Docker infrastructure stack')
    parser.add_argument('--install-dir',required=True)
    parser.add_argument('action',nargs='?',choices=COMMANDS)
    parser.add_argument('arguments',nargs='*')
    parser.add_argument('--confirm',help='Destructive approval: exact project ID from version')
    parser.add_argument('--force',action='store_true',help='Force certificate renewal')
    parser.add_argument('--http-port',type=int);parser.add_argument('--https-port',type=int)
    args=parser.parse_args()
    try:
        path=i.validate_install_dir(args.install_dir)
        if os.geteuid()!=0:raise i.InstallError('Run with sudo/root')
        if args.action is None:
            if not sys.stdin.isatty():parser.print_help();return 0
            menu(args)
        if args.action=='help':parser.print_help();return 0
        if args.action=='self-update':self_update(path);return 0
        with i.operation_lock(path) as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            cfg=i.load(path);action=args.action;values=args.arguments
            if action in ('restore','update','upgrade','import-sql'):approval(args,cfg['project'])
            expected={'restore':1,'update':2,'export-sql':1,'export-table-sql':2,'import-sql':1,'apply-domain':1,'cert-install':3}
            if action in expected and len(values)!=expected[action]:raise i.InstallError(f'{action} requires {expected[action]} argument(s); see help')
            if action=='version':print(json.dumps({'installer':i.VERSION,**cfg},indent=2))
            elif action in ('status','logs','start','stop','restart'):
                allowed=('php','db','redis','nginx','certbot')
                if any(x not in allowed for x in values):raise i.InstallError('Unknown service')
                if action=='status':print(i.compose(path,'ps','--all',*values))
                elif action=='logs':print(i.compose(path,'logs','--tail','100',*values))
                elif action=='stop':i.compose(path,'stop','--timeout','60',*values)
                else:
                    if action=='restart':i.compose(path,'stop','--timeout','60',*values)
                    i.compose(path,'up','-d','--wait','--wait-timeout','120',*values)
                    if not values:i.validate_runtime(path,cfg)
                    print('Started '+(', '.join(values) if values else 'healthy stack'))
            elif action=='health':i.validate_runtime(path,cfg);print('Healthy')
            elif action=='backup':
                if len(values)>1:raise i.InstallError('backup accepts one optional destination directory')
                print(backup(path,cfg,Path(values[0]).absolute() if values else None))
            elif action=='restore':restore(path,Path(values[0]).resolve(),cfg)
            elif action=='update':update(path,cfg,*values)
            elif action=='upgrade':upgrade(path,cfg)
            elif action=='export-sql':export_sql(path,values[0])
            elif action=='export-table-sql':export_sql(path,*values)
            elif action=='import-sql':import_sql(path,cfg,values[0])
            elif action=='apply-config':apply_config(path,cfg)
            elif action=='configure-web':configure_web(path,cfg,values,args)
            else:
                import web
                if action=='apply-domain':cfg['domain']=web.domain(values[0],cfg.get('tls_mode')=='letsencrypt');web.apply(path,cfg)
                elif action=='cert-install':cfg.update(domain=web.domain(values[0]),tls_mode='own',https_port=cfg.get('https_port',443));web.apply(path,cfg,values[1],values[2])
                elif action=='cert-generate':web.generate(path,cfg);atomic(path/'config/nginx.conf',web.nginx(cfg));i.compose(path,'exec','-T','nginx','nginx','-t');i.compose(path,'exec','-T','nginx','nginx','-s','reload');i.validate_runtime(path,cfg)
                elif action=='cert-renew':web.renew(path,cfg,args.force)
                elif action in ('cert-cron-add','cert-cron-remove'):
                    if cfg.get('tls_mode')!='letsencrypt':raise i.InstallError("Automatic renewal requires Let's Encrypt mode")
                    web.stop_renewal(path,cfg);cfg['auto_renew']=action=='cert-cron-add'
                    atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n');atomic(path/'compose.yaml',i.render_compose(cfg));i.compose(path,'up','-d','--wait');i.validate_runtime(path,cfg)
                    print('Container renewal '+('enabled' if cfg['auto_renew'] else 'disabled')+'; host cron unchanged')
        return 0
    except (i.InstallError,OSError,ValueError,KeyError,EOFError,subprocess.SubprocessError,tarfile.TarError) as exc:
        print('ADMIN ERROR: '+str(exc),file=sys.stderr);return 1

if __name__=='__main__':
    sys.modules['admin']=sys.modules[__name__]
    sys.exit(main())
