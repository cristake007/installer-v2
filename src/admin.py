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
import tarfile
import tempfile
import time

import installer as i

COMMANDS=('start','stop','restart','status','health','logs','version','backup','restore','update')
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


def backup(path,cfg):
    i.validate_runtime(path,cfg)
    size=sum(f.stat().st_size for root in SNAPSHOT for f in (path/root).rglob('*') if f.is_file())
    if shutil.disk_usage(path/'backups').free < size+128*1024**2:
        raise i.InstallError('Insufficient disk space for backup')
    target=path/'backups'/str(time.time_ns());target.mkdir(mode=0o700)
    try:
        # Clean shutdown gives PostgreSQL and Redis consistent on-disk snapshots.
        i.compose(path,'stop','--timeout','60')
        with tarfile.open(target/'stack.tar','w') as archive:
            for name in SNAPSHOT: archive.add(path/name,arcname=name)
        os.chmod(target/'stack.tar',0o600)
        i.safe_json(target/'manifest.json',{'format':'stack-backup-v1','project':cfg['project'],'sha256':digest(target/'stack.tar')})
        verify(target,cfg['project'])
        return target
    finally:
        i.compose(path,'up','-d','--wait','--wait-timeout','120')
        i.validate_runtime(path,cfg)


def verify(source,project):
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('format')!='stack-backup-v1' or manifest.get('project')!=project:
        raise i.InstallError('Backup format or installation identity mismatch')
    if digest(source/'stack.tar')!=manifest.get('sha256'): raise i.InstallError('Backup checksum mismatch')
    with tarfile.open(source/'stack.tar') as archive:
        seen=set()
        for member in archive.getmembers():
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or p.parts[0] not in SNAPSHOT or member.name in seen:
                raise i.InstallError('Unsafe backup path')
            if not (member.isfile() or member.isdir()) or member.mode & 0o6000:
                raise i.InstallError('Unsupported backup entry (links and special files are rejected)')
            seen.add(member.name)
        if not set(SNAPSHOT).issubset(seen) or not {'data/public','data/postgres','data/redis','config/nginx.conf'}.issubset(seen):
            raise i.InstallError('Incomplete backup')
        cfg=json.load(archive.extractfile('settings.json'))
        if cfg.get('format')!='infrastructure-v1' or cfg.get('project')!=project:
            raise i.InstallError('Invalid backup settings')
        i.validate_images(cfg['images'])
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
        with tarfile.open(source/'stack.tar') as archive: archive.extractall(stage)
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


def main():
    parser=argparse.ArgumentParser(description='Manage this Docker infrastructure stack')
    parser.add_argument('--install-dir',required=True)
    parser.add_argument('action',choices=COMMANDS)
    parser.add_argument('arguments',nargs='*')
    parser.add_argument('--confirm',help='Restore/update approval: exact project ID from version')
    args=parser.parse_args();path=i.validate_install_dir(args.install_dir)
    try:
        if os.geteuid()!=0: raise i.InstallError('Run with sudo/root')
        with i.operation_lock(path) as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            cfg=i.load(path)
            if args.action in ('restore','update') and args.confirm!=cfg['project']:
                raise i.InstallError('Explicit approval required: --confirm '+cfg['project'])
            if args.action=='version': print(json.dumps({'installer':i.VERSION,**cfg},indent=2))
            elif args.action=='status': print(i.compose(path,'ps','--all'))
            elif args.action=='logs':
                if any(x not in ('php','db','redis','nginx') for x in args.arguments): raise i.InstallError('Unknown service')
                print(i.compose(path,'logs','--tail','100',*args.arguments))
            elif args.action=='stop': i.compose(path,'stop','--timeout','60')
            elif args.action in ('start','restart','health'):
                if args.action=='restart': i.compose(path,'stop','--timeout','60')
                if args.action!='health': i.compose(path,'up','-d','--wait','--wait-timeout','120')
                i.validate_runtime(path,cfg);print('Healthy')
            elif args.action=='backup': print(backup(path,cfg))
            elif args.action=='restore':
                if len(args.arguments)!=1: raise i.InstallError('restore requires a backup directory')
                restore(path,Path(args.arguments[0]).resolve(),cfg)
            elif args.action=='update':
                if len(args.arguments)!=2: raise i.InstallError('update requires SERVICE VERSIONED_IMAGE')
                update(path,cfg,*args.arguments)
        return 0
    except (i.InstallError,OSError,ValueError,KeyError,tarfile.TarError) as exc:
        print('ADMIN ERROR: '+str(exc),file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())
