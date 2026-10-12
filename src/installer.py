#!/usr/bin/env python3
"""Install a small Docker infrastructure stack. Standard library only."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time

VERSION = '0.3.0'
SUPPORTED = {'ubuntu': {'22.04', '24.04'}, 'debian': {'12', '13'}}
DEFAULT_IMAGES = {'php': 'php:8.4-fpm-alpine', 'postgres': 'postgres:18-alpine', 'redis': 'redis:8-alpine', 'nginx': 'nginx:1.28-alpine'}
REPO = Path(__file__).resolve().parents[1]


class InstallError(Exception):
    pass


def docker_environment(env: dict | None = None) -> dict:
    result = dict(os.environ if env is None else env)
    for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','COMPOSE_FILE','COMPOSE_PROFILES','COMPOSE_PROJECT_NAME'):
        result.pop(key, None)
    return result


def docker_command(*args: str) -> list[str]:
    return ['docker','--host','unix:///var/run/docker.sock',*args]


def run(cmd: list[str], *, timeout: int = 120, capture: bool = True, env: dict | None = None) -> str:
    if cmd[0] == 'docker':
        cmd = docker_command(*cmd[1:])
        env = docker_environment(env)
    try:
        proc = subprocess.run(cmd, check=True, text=True, env=env,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              timeout=timeout)
        return proc.stdout.strip() if capture else ''
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, 'stderr', '') or ''
        # Never include the command: arguments may hold credentials.
        raise InstallError(f'Command failed ({cmd[0]}): {stderr[-400:]}') from exc


def read_os_release(path: Path = Path('/etc/os-release')) -> tuple[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k] = v.strip().strip('"').strip("'")
    return values.get('ID', ''), values.get('VERSION_ID', '')


def validate_host(os_release: Path = Path('/etc/os-release')) -> None:
    system, release = read_os_release(os_release)
    if release not in SUPPORTED.get(system, set()):
        raise InstallError(f'Unsupported OS: {system} {release}. Tested families: Ubuntu 22.04/24.04 and Debian 12/13.')
    if not sys.platform.startswith('linux'):
        raise InstallError('Only Linux hosts are supported.')


def validate_install_dir(text: str) -> Path:
    path = Path(text)
    if not path.is_absolute() or str(path) == '/' or '..' in path.parts:
        raise InstallError('Install directory must be an absolute, non-root path without ..')
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise InstallError('Install directory must not be a symlink')
    return path


def validate_port(port: int) -> None:
    if not 1024 <= port <= 65535 and port not in {80, 443}:
        raise InstallError('Port must be 80, 443 or between 1024 and 65535')


def is_port_free(port: int, host: str = '0.0.0.0') -> bool:
    # Binding is not a reservation; Docker still performs final collision check.
    probes = [(socket.AF_INET, (host, port))]
    if host == '0.0.0.0':
        probes.append((socket.AF_INET6, ('::', port)))
    for family, addr in probes:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind(addr)
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (97, 99):  # IPv6 not supported
                continue
            return False
    return True


def image_digest(image: str) -> str:
    run(['docker', 'pull', image], timeout=480, capture=False)
    if '@sha256:' in image:
        return image
    digests = run(['docker', 'image', 'inspect', image, '--format', '{{json .RepoDigests}}'])
    refs = json.loads(digests)
    if not refs:
        raise InstallError(f'Cannot resolve digest for {image}')
    target_repo = image.rsplit(':', 1)[0]
    normalized_target = target_repo if '/' in target_repo else 'library/' + target_repo
    match = next((ref for ref in refs if ref.split('@', 1)[0].removeprefix('docker.io/') in (target_repo, normalized_target)), None)
    if not match or not re.search(r'@sha256:[a-fA-F0-9]{64}$', match):
        raise InstallError(f'No trustworthy digest found for image {image}')
    return match


def docker_available() -> bool:
    try:
        engine = run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=15)
        plugin = run(['docker', 'compose', 'version', '--short'], timeout=15).lstrip('v')
        if int(engine.split('.')[0]) < 24 or tuple(int(x) for x in plugin.split('.')[:2]) < (2,20):
            return False
        return True
    except (InstallError, ValueError):
        return False



def validate_docker_key(output: str) -> None:
    rows=[line.split(':') for line in output.splitlines()]
    primary=[row for row in rows if row[0]=='pub']
    fingerprints=[row[9].upper() for row in rows if row[0]=='fpr' and len(row)>9]
    if len(primary)!=1 or not fingerprints or fingerprints[0]!='9DC858229FC7DD38854AE2D88D81803C0EBFCD88':
        raise InstallError('Docker repository signing key did not match the single expected primary key')

def install_docker(system: str, release: str) -> None:
    """Install from Docker's official apt repository only when explicitly authorized."""
    if shutil.which('docker'):
        raise InstallError('Docker binary exists but the daemon/Compose is unavailable. Refusing to replace it.')
    if any(shutil.which(name) for name in ('containerd','runc','podman')):
        raise InstallError('Existing container runtime detected; operator must provision Docker without replacing unrelated services.')
    if not shutil.which('apt-get'):
        raise InstallError('apt-get is required to bootstrap Docker.')
    run(['apt-get', 'update'], timeout=300, capture=False)
    run(['env', 'NEEDRESTART_MODE=l', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-remove', '--no-install-recommends', 'ca-certificates', 'curl', 'gnupg'], timeout=300, capture=False)
    # No curl-to-shell. Download Docker repository signing key and validate via gpg.
    keyring = Path('/etc/apt/keyrings')
    keyring.mkdir(parents=True, exist_ok=True)
    keyfile = keyring / 'docker-fluidweb.asc'
    if keyfile.exists():
        raise InstallError('Existing installer Docker key: review manually before host bootstrap')
    host = 'ubuntu' if system == 'ubuntu' else 'debian'
    run(['curl', '--proto', '=https', '--tlsv1.2', '-fsSL',
         f'https://download.docker.com/linux/{host}/gpg', '-o', str(keyfile)], timeout=60)
    # Docker's published signing key fingerprint. Fail closed if the download is replaced.
    fingerprint = run(['gpg', '--show-keys', '--with-colons', '--fingerprint', str(keyfile)])
    validate_docker_key(fingerprint)
    os.chmod(keyfile, 0o644)
    arch = run(['dpkg', '--print-architecture'])
    distro = {'ubuntu': {'22.04': 'jammy', '24.04': 'noble'},
              'debian': {'12': 'bookworm', '13': 'trixie'}}[system][release]
    source = Path('/etc/apt/sources.list.d/docker-fluidweb.sources')
    if source.exists():
        raise InstallError(f'Existing Docker apt source {source}; review manually before continuing.')
    source.write_text(f'Types: deb\nURIs: https://download.docker.com/linux/{host}\nSuites: {distro}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {keyfile}\n')
    run(['apt-get', 'update'], timeout=300, capture=False)
    run(['env', 'NEEDRESTART_MODE=l', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-remove', '--no-install-recommends',
         'docker-ce', 'docker-ce-cli', 'containerd.io',
         'docker-buildx-plugin', 'docker-compose-plugin'], timeout=600, capture=False)
    if not docker_available():
        raise InstallError('Docker installed but Engine/Compose not operational. Check service status.')


def project_name(path: Path) -> str:
    import hashlib
    return 'fluidweb_' + hashlib.sha256(str(path).encode()).hexdigest()[:12]


def protected_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Exclusive write: never overwrite a pre-existing user file."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Keep partial file for investigation, not silently destroy state.
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def safe_json(path: Path, obj: dict) -> None:
    protected_write(path, json.dumps(obj, indent=2, sort_keys=True) + '\n')


def compose_environment(path: Path) -> dict:
    env = docker_environment()
    for line in (path / '.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        if '=' not in line: raise InstallError('Invalid managed environment file')
        key, value = line.split('=', 1)
        if not re.fullmatch('[A-Z_][A-Z0-9_]*', key): raise InstallError('Invalid environment key')
        env[key] = value
    return env


def compose(path: Path, *args: str, timeout: int = 300) -> str:
    return run(['docker', 'compose', '--project-name', project_name(path), '--project-directory', str(path), '--env-file', str(path / '.env'),
                '-f', str(path / 'compose.yaml'), *args], timeout=timeout, env=compose_environment(path))


def operation_lock(path: Path):
    lock_path = path.parent / ('.' + path.name + '.installer.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, 'a+')

# Infrastructure only: official images, a shared document root, no deployment hooks.
NGINX_CONFIG = '''server {
    listen 80 default_server;
    server_name _;
    root /var/www/html/public;
    index index.php;
    location / { try_files $uri $uri/ =404; }
    location = /health { rewrite ^ /health.php last; }
    location ~ /\\. { deny all; }
    location ~ \\.php$ {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass php:9000;
    }
}
'''


def validate_images(images):
    if set(images) != set(DEFAULT_IMAGES):
        raise InstallError('Invalid infrastructure image settings')
    for service, image in images.items():
        if not re.fullmatch(re.escape(service) + r'@sha256:[a-f0-9]{64}', image):
            # Docker normalizes short official repositories to library/<name>.
            if not re.fullmatch('(?:docker.io/)?library/' + re.escape(service) + r'@sha256:[a-f0-9]{64}', image):
                raise InstallError('Expected an official pinned image for '+service)


def render_compose(cfg):
    def service(name):
        return {'image': cfg['images'][name], 'restart': 'unless-stopped'}
    health = lambda cmd: {'test': cmd, 'interval': '2s', 'timeout': '5s', 'retries': 30}
    php = service('php')
    php.update(volumes=['./data/public:/var/www/html/public'], networks=['frontend','backend'],
               healthcheck=health(['CMD','php','-r','exit(is_resource(fsockopen("127.0.0.1",9000)) ? 0 : 1);']))
    if cfg['web_mode'] == 'external':
        php['ports'] = [f"127.0.0.1:{cfg['fpm_port']}:9000"]
    db = service('postgres')
    db.update(environment={'POSTGRES_USER':'fluidweb','POSTGRES_DB':'fluidweb','POSTGRES_PASSWORD':'${POSTGRES_PASSWORD}'},
              volumes=['./data/postgres:/var/lib/postgresql'], networks=['backend'],
              healthcheck=health(['CMD','pg_isready','-U','fluidweb','-d','fluidweb']))
    redis = service('redis')
    redis.update(command=['redis-server','--appendonly','yes'], volumes=['./data/redis:/data'], networks=['backend'],
                 healthcheck=health(['CMD','redis-cli','ping']))
    services = {'php':php,'db':db,'redis':redis}
    if cfg['web_mode'] == 'nginx':
        nginx = service('nginx')
        nginx.update(ports=[f"{cfg['http_bind']}:{cfg['http_port']}:80"],
                     volumes=['./data/public:/var/www/html/public:ro','./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro'],
                     networks=['frontend'], depends_on={'php':{'condition':'service_healthy'}})
        services['nginx'] = nginx
    return json.dumps({'services':services,'networks':{'frontend':{},'backend':{'internal':True}}},indent=2)+'\n'


def load(path):
    validate_install_dir(str(path))
    for name in ('settings.json','.env','compose.yaml','config','data','data/public','data/postgres','data/redis','backups','src'):
        if (path/name).is_symlink() or not (path/name).exists():
            raise InstallError('Missing or unsafe installation path: '+name)
    cfg = json.loads((path/'settings.json').read_text())
    if cfg.get('format') != 'infrastructure-v1' or cfg.get('project') != project_name(path):
        raise InstallError('Unknown installation format. Existing application installers need a separate manual migration; nothing was changed.')
    if cfg.get('web_mode') not in ('nginx','external'):
        raise InstallError('Invalid web-server mode')
    validate_images(cfg['images'])
    for key in ('http_port','fpm_port'): validate_port(cfg[key])
    import ipaddress
    ipaddress.IPv4Address(cfg['http_bind'])
    if not re.fullmatch(r'POSTGRES_PASSWORD=[a-f0-9]{64}\n', (path/'.env').read_text()):
        raise InstallError('Invalid saved credentials; refusing ambient environment values')
    if (path/'.env').stat().st_mode & 0o077:
        raise InstallError('Credential file must be private (0600)')
    if (path/'compose.yaml').read_text() != render_compose(cfg):
        raise InstallError('Compose configuration differs from saved settings; review it before continuing')
    return cfg


def validate_runtime(path, cfg):
    compose(path,'exec','-T','db','pg_isready','-U','fluidweb','-d','fluidweb')
    if compose(path,'exec','-T','redis','redis-cli','ping') != 'PONG':
        raise InstallError('Redis health failed')
    if compose(path,'exec','-T','php','php','-r','echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;') != '8.4':
        raise InstallError('PHP 8.4 required')
    for _ in range(15):
        try:
            if cfg['web_mode']=='external':
                from fastcgi import probe
                probe(cfg['fpm_port'])
            else:
                import urllib.request
                host = cfg['http_bind'] if cfg['http_bind']!='0.0.0.0' else '127.0.0.1'
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'http://{host}:{cfg["http_port"]}/health',timeout=3) as response:
                    if response.read().strip()!=b'PHP 8.4 OK': raise OSError('Unexpected PHP health response')
            return
        except OSError:
            time.sleep(1)
    raise InstallError('PHP-FPM HTTP/FastCGI health failed; check command.sh logs')


def install(args):
    validate_host()
    if os.geteuid()!=0: raise InstallError('Run the installer with sudo/root')
    path=validate_install_dir(args.install_dir)
    if not path.is_relative_to('/opt') or path == Path('/opt'):
        raise InstallError('Choose an installation directory under /opt/')
    if not path.parent.is_dir(): raise InstallError('Create the installation parent directory first')
    if not docker_available():
        if args.install_docker: install_docker(*read_os_release())
        else: raise InstallError('Docker Engine 24+ and Compose 2.20+ must be operational. Use --install-docker for optional installation.')
    if args.preflight:
        print('Supported host and Docker prerequisites passed'); return
    with operation_lock(path) as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if path.exists():
            cfg=load(path)
        else:
            import ipaddress
            ipaddress.IPv4Address(args.http_bind)
            validate_port(args.http_port); validate_port(args.fpm_port)
            port=args.http_port if args.web_mode=='nginx' else args.fpm_port
            host=args.http_bind if args.web_mode=='nginx' else '127.0.0.1'
            if not is_port_free(port,host): raise InstallError(f'Port {host}:{port} is occupied; choose another port')
            if shutil.disk_usage(path.parent).free < 1024**3: raise InstallError('At least 1 GiB free disk required')
            images={k:image_digest(v) for k,v in DEFAULT_IMAGES.items()}
            cfg={'format':'infrastructure-v1','project':project_name(path),'web_mode':args.web_mode,
                 'http_bind':args.http_bind,'http_port':args.http_port,'fpm_port':args.fpm_port,'images':images}
            path.mkdir(mode=0o755)
            for name in ('config','data','data/public','data/postgres','data/redis','backups','src'):
                (path/name).mkdir(mode=0o755 if name in ('data','data/public') else 0o700)
            safe_json(path/'settings.json',cfg)
            protected_write(path/'.env','POSTGRES_PASSWORD='+secrets.token_hex(32)+'\n')
            protected_write(path/'compose.yaml',render_compose(cfg))
            protected_write(path/'config/nginx.conf',NGINX_CONFIG,0o644)
            endpoint='<?php header("Content-Type: text/plain"); echo "PHP ".PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION." OK\\n";\n'
            for name in ('health.php','index.php'): protected_write(path/'data/public'/name,endpoint,0o644)
            for service,user,directory in (('php','www-data','public'),('postgres','postgres','postgres'),('redis','redis','redis')):
                uid=int(run(['docker','run','--rm','--network','none','--entrypoint','id',images[service],'-u',user]))
                gid=int(run(['docker','run','--rm','--network','none','--entrypoint','id',images[service],'-g',user]))
                os.chown(path/'data'/directory,uid,gid)
            for name in ('nginx.conf.example','apache.conf.example'):
                protected_write(path/'config'/name,(REPO/'examples'/name).read_text().replace('/opt/fluidweb',str(path)).replace(':9080',':'+str(cfg['fpm_port'])),0o644)
        # Refresh installer tooling only; saved credentials, settings and data stay intact.
        for name in ('installer.py','admin.py','fastcgi.py'):
            source=REPO/'src'/name; target=path/'src'/name
            if target.is_symlink(): raise InstallError('Unsafe installer script path')
            if source.resolve()!=target.resolve(): shutil.copyfile(source,target); os.chmod(target,0o600)
        if (REPO/'command.sh').resolve()!=(path/'command.sh').resolve():
            shutil.copyfile(REPO/'command.sh',path/'command.sh'); os.chmod(path/'command.sh',0o750)
        compose(path,'up','-d','--wait','--wait-timeout','120')
        validate_runtime(path,cfg)
        print(f'Infrastructure healthy at {path}. Credentials: {path}/.env (private).')
        if cfg['web_mode']=='external': print('Configure your own web server using config/*.example; only loopback FastCGI was verified.')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Install PHP 8.4-FPM, PostgreSQL 18, Redis and optional Nginx')
    parser.add_argument('--install-dir',default='/opt/fluidweb')
    parser.add_argument('--web-mode',choices=('nginx','external'))
    parser.add_argument('--http-bind',default='0.0.0.0')
    parser.add_argument('--http-port',type=int,default=80)
    parser.add_argument('--fpm-port',type=int,default=9080)
    parser.add_argument('--install-docker',action='store_true')
    parser.add_argument('--preflight',action='store_true')
    args=parser.parse_args(argv)
    if not args.web_mode:
        if sys.stdin.isatty() and not args.preflight and not Path(args.install_dir).exists():
            args.install_dir=input(f'Installation directory [{args.install_dir}]: ').strip() or args.install_dir
            args.web_mode='external' if input('Web server: [1] Nginx container, [2] external (default 1): ').strip()=='2' else 'nginx'
            key='http_port' if args.web_mode=='nginx' else 'fpm_port'
            answer=input(f'Port [{getattr(args,key)}]: ').strip()
            if answer:
                try: setattr(args,key,int(answer))
                except ValueError: parser.error('Port must be a number')
            if not docker_available() and not args.install_docker:
                args.install_docker=input('Install Docker from its official repository? [y/N]: ').strip().lower()=='y'
        else: args.web_mode='nginx'
    try:
        install(args); return 0
    except (InstallError,OSError,ValueError,KeyError) as exc:
        print('INSTALL ERROR: '+str(exc),file=sys.stderr); return 1

if __name__=='__main__': sys.exit(main())
