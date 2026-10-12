#!/usr/bin/env python3
"""Installation-scoped operations. No global Docker mutations."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import tempfile

import installer as i

COMMANDS = ['start','stop','restart','status','health','logs','version','backup','restore','upgrade','recover','renew-tls']
FILES = ('settings.json', '.env', 'compose.yaml', 'config/nginx.conf')



def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()

def atomic(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    tmp = Path(temporary)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory=os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def seal(path):
    atomic(path / 'integrity.json', json.dumps({f: digest(path/f) for f in FILES}))


def load(path):
    i.validate_install_dir(str(path))
    cfg = json.loads((path/'settings.json').read_text())
    if not isinstance(cfg, dict):
        raise i.InstallError('Settings must be a JSON object')
    if cfg.get('web_mode') not in ('nginx','external') or not isinstance(cfg.get('images'),dict) or set(cfg['images']) != {'app','postgres','redis','nginx'}:
        raise i.InstallError('Invalid installation runtime settings')
    if cfg.get('project') != i.project_name(path) or cfg.get('version') != 1:
        raise i.InstallError('Invalid installation identity/version')
    for relative in ('src', 'config', 'data', 'backups', 'data/postgres', 'data/redis', 'data/public', 'data/storage', 'data/custom', 'data/cache'):
        target = path/relative
        if target.is_symlink() or not target.is_dir():
            raise i.InstallError('Invalid installation directory: '+relative)
    hashes = json.loads((path/'integrity.json').read_text())
    if not isinstance(hashes,dict) or set(hashes) != set(FILES):
        raise i.InstallError('Incomplete integrity record')
    for f, expected in hashes.items():
        if (path/f).is_symlink() or digest(path/f) != expected:
            raise i.InstallError('Configuration integrity mismatch: ' + f)
    for image in cfg['images'].values():
        i.validate_app_image(image)
    if (path/'.env').stat().st_mode & 0o077:
        raise i.InstallError('Credentials permissions must be 0600')
    return cfg


def initialize_public(path, cfg):
    for service, user, directory in (('postgres','postgres','postgres'),('redis','redis','redis')):
        uid = i.run(['docker','run','--rm','--entrypoint','id',cfg['images'][service],'-u',user])
        gid = i.run(['docker','run','--rm','--entrypoint','id',cfg['images'][service],'-g',user])
        os.chown(path/'data'/directory,int(uid),int(gid))
    if (path/'public.initialized').exists():
        return
    # Copy baked assets without mounting an empty host directory over the image.
    cid = i.run(['docker', 'create', '--label', 'installer.fixture-owner='+cfg['project'], cfg['images']['app']])
    try:
        i.run(['docker', 'cp', cid+':/var/www/html/public/.', str(path/'data/public')])
        if any(item.is_symlink() for item in (path/'data/public').rglob('*')):
            raise i.InstallError('Symlinks in baked public assets are unsafe')
        uid = i.run(['docker', 'run', '--rm', '--entrypoint', 'id', cfg['images']['app'], '-u', 'www-data'])
        gid = i.run(['docker', 'run', '--rm', '--entrypoint', 'id', cfg['images']['app'], '-g', 'www-data'])
        for name in ('storage', 'custom', 'cache'):
            os.chown(path/'data'/name, int(uid), int(gid))
        for root, dirs, files in os.walk(path/'data/public'):
            os.chmod(root, 0o755)
            for name in files:
                os.chmod(Path(root)/name, 0o644)
        for root, dirs, files in os.walk(path/'data/public'):
            os.chown(root,int(uid),int(gid))
            for name in files:
                os.chown(Path(root)/name,int(uid),int(gid))
        assets={str(f.relative_to(path/'data/public')): digest(f) for f in (path/'data/public').rglob('*') if f.is_file()}
        atomic(path/'public-assets.json',json.dumps(assets))
        i.protected_write(path/'public.initialized', '1\n')
    finally:
        i.run(['docker', 'rm', cid])



def refresh_assets(path, cfg):
    previous=json.loads((path/'public-assets.json').read_text())
    if any(item.is_symlink() for item in (path/'data/public').rglob('*')):
        raise i.InstallError('Symlinks in public assets are unsafe for upgrade')
    for relative in previous:
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise i.InstallError('Invalid public asset tracking path')
    with tempfile.TemporaryDirectory(dir=path) as temporary:
        stage=Path(temporary)
        cid=i.run(['docker','create',cfg['images']['app']])
        try:
            i.run(['docker','cp',cid+':/var/www/html/public/.',str(stage)])
        finally:
            i.run(['docker','rm',cid])
        assets={}
        for source in stage.rglob('*'):
            if source.is_symlink(): raise i.InstallError('Application public assets cannot contain symlinks')
            if not source.is_file(): continue
            relative=str(source.relative_to(stage));target=path/'data/public'/relative
            if target.exists() and (relative not in previous or digest(target)!=previous[relative]):
                raise i.InstallError('Public asset conflicts with operator/upload file: '+relative)
            assets[relative]=digest(source)
        uid=int(i.run(['docker','run','--rm','--entrypoint','id',cfg['images']['app'],'-u','www-data']))
        gid=int(i.run(['docker','run','--rm','--entrypoint','id',cfg['images']['app'],'-g','www-data']))
        for relative in assets:
            target=path/'data/public'/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(stage/relative,target)
            os.chmod(target,0o644);os.chown(target,uid,gid)
        for relative,expected in previous.items():
            target=path/'data/public'/relative
            if relative not in assets and target.is_file() and digest(target)==expected:
                target.unlink()
        atomic(path/'public-assets.json',json.dumps(assets))

def dbcmd(path, *args):
    return ['docker', 'compose', '--project-directory', str(path), '--env-file', str(path/'.env'), '-f', str(path/'compose.yaml'), 'exec', '-T', 'db', *args]


def verify(backup):
    manifest = json.loads((backup/'manifest.json').read_text())
    if manifest.get('version') != 1:
        raise i.InstallError('Unsupported backup version')
    if set(manifest['files']) != {'database.dump', 'files.tar'}:
        raise i.InstallError('Invalid backup manifest')
    for name, expected in manifest['files'].items():
        if digest(backup/name) != expected:
            raise i.InstallError('Backup integrity failure: '+name)
    with tarfile.open(backup/'files.tar') as archive:
        seen = set()
        for member in archive.getmembers():
            if member.name in seen:
                raise i.InstallError('Duplicate archive member')
            seen.add(member.name)
            p = Path(member.name)
            allowed = set(FILES) | {'integrity.json', 'public-assets.json'}
            if member.name not in allowed and not any(member.name == root or member.name.startswith(root+'/') for root in ('data/public','data/storage','data/custom','data/cache','data/redis','config/tls')):
                raise i.InstallError('Unexpected archive member')
            if member.mode & 0o6000 or p.is_absolute() or '..' in p.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise i.InstallError('Unsafe backup archive')
        required=set(FILES) | {'integrity.json','public-assets.json','data/public','data/storage','data/custom','data/cache','data/redis'}
        if not required.issubset(seen):
            raise i.InstallError('Incomplete backup archive')
        cfg=json.load(archive.extractfile('settings.json'))
        integrity=json.load(archive.extractfile('integrity.json'))
        if cfg.get('version') != 1 or cfg.get('project') != manifest.get('project') or cfg.get('images') != manifest.get('images'):
            raise i.InstallError('Inconsistent backup metadata')
        for image in cfg['images'].values():
            i.validate_app_image(image)
        for name in FILES:
            if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != integrity.get(name):
                raise i.InstallError('Backup configuration integrity failure: '+name)
        if cfg.get('tls') and not {'config/tls/fullchain.pem','config/tls/key.pem'}.issubset(seen):
            raise i.InstallError('Incomplete TLS backup')
    return manifest


def backup(path, cfg):
    i.validate_runtime(path, cfg)
    size=int(i.compose(path,'exec','-T','db','psql','-U','fluidweb','-d','fluidweb','-At','-c',"SELECT pg_database_size('fluidweb')"))
    size+=sum(f.stat().st_size for name in ('public','storage','custom','cache','redis') for f in (path/'data'/name).rglob('*') if f.is_file())
    if shutil.disk_usage(path/'backups').free < size*2 + 256*1024**2:
        raise i.InstallError('Insufficient free disk for backup; provision additional storage')
    target = path/'backups'/str(time.time_ns())
    target.mkdir(mode=0o700)
    # Quiesce all application writers; pg_dump supplies a transactional snapshot.
    try:
        i.compose(path, 'stop', 'app', *(['nginx'] if cfg['web_mode']=='nginx' else []))
        with (target/'database.dump').open('wb') as out:
            subprocess.run(dbcmd(path, 'pg_dump', '-U', 'fluidweb', '-d', 'fluidweb', '-Fc'), stdout=out, check=True, timeout=300)
        i.compose(path, 'stop', 'redis')
        with tarfile.open(target/'files.tar', 'w') as archive:
            for name in (*FILES, 'integrity.json', 'public-assets.json', 'data/public', 'data/storage', 'data/custom', 'data/cache', 'data/redis', *(['config/tls'] if cfg.get('tls') else [])):
                archive.add(path/name, arcname=name)
        for name in ('database.dump','files.tar'):
            os.chmod(target/name,0o600)
            with (target/name).open('rb') as stream:
                os.fsync(stream.fileno())
        parent=os.open(target.parent,os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        manifest = {'version': 1, 'project': cfg['project'], 'images': cfg['images'], 'files': {name: digest(target/name) for name in ('database.dump','files.tar')}}
        i.safe_json(target/'manifest.json', manifest)
        for item in target.iterdir():
            os.chmod(item, 0o600)
        verify(target)
        return target
    finally:
        i.compose(path, 'up', '-d', '--wait')
        i.validate_runtime(path, cfg)


def restore(path, source):
    manifest = verify(source)
    for name in ('data','config','backups'):
        if (path/name).is_symlink() or not (path/name).is_dir():
            raise i.InstallError('Unsafe restore parent directory: '+name)
    with tarfile.open(source/'files.tar') as archive:
        cfg=json.load(archive.extractfile('settings.json'))
        environment=archive.extractfile('.env').read().decode()
        composition=archive.extractfile('compose.yaml').read().decode()
    if cfg.get('project') != i.project_name(path):
        raise i.InstallError('Invalid installation identity')
    if manifest['project'] != cfg['project']:
        raise i.InstallError('Backup belongs to a different installation')
    # Validate the database archive before touching the running application.
    with (source/'database.dump').open('rb') as inp:
        subprocess.run(['docker','run','--rm','-i','--network','none','--entrypoint','pg_restore',cfg['images']['postgres'],'--list'], stdin=inp, stdout=subprocess.DEVNULL, check=True)
    atomic(path/'settings.json',json.dumps(cfg))
    atomic(path/'.env',environment)
    atomic(path/'compose.yaml',composition)
    i.compose(path, 'stop', 'app', 'redis', *(['nginx'] if cfg['web_mode']=='nginx' else []))
    for name in ('public', 'storage', 'custom', 'cache', 'redis'):
        target=path/'data'/name
        if target.is_symlink():
            raise i.InstallError('Refusing symlinked data directory')
        if target.exists():
            shutil.rmtree(target)
    with tarfile.open(source/'files.tar') as archive:
        archive.extractall(path, filter='fully_trusted')
    # Recreate this application's database, removing all migration-created schemas.
    i.compose(path, 'exec', '-T', 'db', 'dropdb', '-U', 'fluidweb', '--if-exists', '--force', 'fluidweb')
    i.compose(path, 'exec', '-T', 'db', 'createdb', '-U', 'fluidweb', '-O', 'fluidweb', 'fluidweb')
    with (source/'database.dump').open('rb') as inp:
        subprocess.run(dbcmd(path, 'pg_restore', '-U', 'fluidweb', '-d', 'fluidweb', '--exit-on-error'), stdin=inp, check=True, timeout=300)
    i.compose(path, 'up', '-d', '--wait', '--force-recreate')
    i.validate_runtime(path, load(path))


def upgrade(path, cfg, image):
    i.validate_app_image(image)
    image = i.image_digest(image)
    # Explicit image compatibility hook executes without access to current data.
    i.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'php', image, '/opt/installer/compatibility.php', cfg['images']['app']])
    saved = backup(path, cfg)
    i.safe_json(path/'upgrade.json', {'backup': str(saved), 'from': cfg['images']['app'], 'to': image})
    try:
        i.compose(path, 'stop', 'app', *(['nginx'] if cfg['web_mode']=='nginx' else []))
        old=cfg['images']['app']; cfg['images']['app']=image
        atomic(path/'settings.json', json.dumps(cfg, indent=2)+'\n')
        atomic(path/'.env', (path/'.env').read_text().replace('APP_IMAGE='+old, 'APP_IMAGE='+image))
        seal(path)
        refresh_assets(path,cfg)
        i.compose(path, 'run', '--rm', '--no-deps', 'app', 'php', '/opt/installer/migrate.php')
        i.compose(path, 'up', '-d', '--wait')
        i.validate_runtime(path, cfg)
    except Exception:
        restore(path, saved)
        (path/'upgrade.json').unlink()
        raise i.InstallError('Upgrade failed; validated pre-upgrade database/files/image restored')
    (path/'upgrade.json').unlink()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--install-dir', required=True)
    parser.add_argument('action', choices=COMMANDS)
    parser.add_argument('arguments', nargs='*')
    parser.add_argument('--confirm', help='Destructive approval: exact installation project ID')
    args=parser.parse_args(); path=Path(args.install_dir)
    try:
        i.validate_install_dir(str(path))
        if os.geteuid() != 0:
            raise i.InstallError('Run administration as root/sudo')
        with i.operation_lock(path) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            action=args.action
            if action=='recover' and any((path/name).exists() for name in ('upgrade.json','restore.json')):
                cfg={'project': i.project_name(path)}
            else:
                cfg=load(path)
            if any((path/name).exists() for name in ('upgrade.json','restore.json')) and action not in ('recover','status','logs','version'):
                raise i.InstallError('Interrupted operation: use recover with --confirm PROJECT')
            if action in ('restore','upgrade','recover') and args.confirm != cfg['project']:
                raise i.InstallError('Explicit independent approval required: --confirm '+cfg['project'])
            if action=='version': print(json.dumps(cfg, indent=2))
            elif action=='status': print(i.compose(path,'ps','--all'))
            elif action=='logs':
                if any(x not in ('app','db','redis','nginx') for x in args.arguments): raise i.InstallError('Unknown log service')
                print(i.compose(path,'logs','--tail=200',*args.arguments))
            elif action=='stop': i.compose(path,'stop')
            elif action in ('start','restart','health'):
                if action=='start': i.compose(path,'up','-d','--wait')
                if action=='restart': i.compose(path,'restart')
                i.validate_runtime(path,cfg); print('Healthy')
            elif action=='backup': print(backup(path,cfg))
            elif action=='restore':
                if len(args.arguments)!=1: raise i.InstallError('restore requires backup directory')
                source=Path(args.arguments[0]); manifest=verify(source)
                if manifest['project'] != cfg['project']: raise i.InstallError('Backup belongs to a different installation')
                saved=backup(path,cfg)
                i.safe_json(path/'restore.json',{'backup':str(saved),'requested':str(source)})
                restore(path,source)
                (path/'restore.json').unlink()
            elif action=='upgrade':
                if len(args.arguments)!=1: raise i.InstallError('upgrade requires immutable image')
                upgrade(path,cfg,args.arguments[0])
            elif action=='recover':
                journal_path=next((path/name for name in ('upgrade.json','restore.json') if (path/name).exists()),None)
                if journal_path is None: raise i.InstallError('No interrupted operation to recover')
                journal=json.loads(journal_path.read_text()); restore(path,Path(journal['backup'])); journal_path.unlink()
            elif action=='renew-tls':
                if not cfg.get('tls') or len(args.arguments)!=2: raise i.InstallError('renew-tls requires certificate and key on a TLS install')
                import ssl
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(*args.arguments)
                atomic(path/'config/tls/fullchain.pem',Path(args.arguments[0]).read_text())
                atomic(path/'config/tls/key.pem',Path(args.arguments[1]).read_text())
                i.compose(path,'exec','-T','nginx','nginx','-t')
                i.compose(path,'exec','-T','nginx','nginx','-s','reload')
        return 0
    except (i.InstallError, OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print('ADMIN ERROR: '+str(exc),file=sys.stderr); return 1

if __name__=='__main__': sys.exit(main())
