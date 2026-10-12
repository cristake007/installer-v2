#!/usr/bin/env python3
"""FluidWeb v2 installer. Standard-library only, safety first."""
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

VERSION = '0.2.0'
SUPPORTED = {'ubuntu': {'22.04', '24.04'}, 'debian': {'12', '13'}}
DEFAULT_IMAGES = {'postgres': 'postgres:18-alpine', 'redis': 'redis:8-alpine', 'nginx': 'nginx:1.28-alpine'}
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


def validate_app_image(image: str) -> None:
    # Never deploy an unversioned/development image to an end user's server.
    if not isinstance(image, str) or not re.fullmatch(r'[a-zA-Z0-9._/:-]+@sha256:[a-fA-F0-9]{64}', image):
        raise InstallError('Application image must use immutable @sha256:<64-hex-digest> syntax.')


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


def settings(args: argparse.Namespace, resolved: dict[str, str]) -> dict:
    return {'version': 1, 'web_mode': args.web_mode,
            'http_port': args.http_port if args.web_mode == 'nginx' else None,
            'fpm_port': args.fpm_port if args.web_mode == 'external' else None,
            'project': project_name(Path(args.install_dir)),
            'images': resolved, 'http_bind': getattr(args, 'http_bind', '0.0.0.0'),
            'tls': bool(getattr(args, 'tls_cert', None)),
            'https_port': getattr(args, 'https_port', 443)}


def make_env(cfg: dict) -> str:
    lines = {'COMPOSE_PROJECT_NAME': cfg['project'],
             'APP_IMAGE': cfg['images']['app'],
             'POSTGRES_IMAGE': cfg['images']['postgres'],
             'REDIS_IMAGE': cfg['images']['redis'],
             'NGINX_IMAGE': cfg['images']['nginx'],
             'POSTGRES_PASSWORD': secrets.token_hex(32),
             'APP_SECRET': secrets.token_hex(32),
             'HTTP_PORT': str(cfg['http_port'] or 8080),
             'FPM_PORT': str(cfg['fpm_port'] or 9080),
             'HTTP_BIND': cfg.get('http_bind', '0.0.0.0'),
             'HTTPS_PORT': str(cfg.get('https_port',443))}
    return ''.join(f'{k}={v}\n' for k, v in lines.items())


def render_compose(cfg: dict) -> str:
    # Files/images are local-to-install-root, no absolute host resource collisions.
    nginx = cfg['web_mode'] == 'nginx'
    app_port = '' if nginx else '    ports:\n      - "127.0.0.1:${FPM_PORT}:9000"\n'
    nginx_service = '''  nginx:
    image: ${NGINX_IMAGE}
    restart: unless-stopped
    ports:
      - "${HTTP_BIND}:${HTTP_PORT}:80"
    volumes:
      - ./data/public:/var/www/html/public:ro
      - ./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - app
    networks:
      - frontend
''' if nginx else ''
    if cfg.get('tls'):
        nginx_service = nginx_service.replace('    volumes:', '      - "${HTTP_BIND}:${HTTPS_PORT}:443"\n    volumes:').replace('    depends_on:', '      - ./config/tls:/etc/nginx/tls:ro\n    depends_on:')
    return '''# Generated by installer-v2. Edit only through a supported upgrade/reconfigure process.
services:
  app:
    image: ${APP_IMAGE}
    restart: unless-stopped
    environment:
      APP_ENV: prod
      APP_DEBUG: "0"
      APP_SECRET: ${APP_SECRET}
      DATABASE_URL: "postgresql://fluidweb:${POSTGRES_PASSWORD}@db:5432/fluidweb?serverVersion=18&charset=utf8"
      REDIS_URL: redis://redis:6379
    healthcheck:
      test: ["CMD", "php", "/opt/installer/health.php"]
      interval: 2s
      timeout: 5s
      retries: 10
      start_period: 5s
    volumes:
      - ./data/public:/var/www/html/public
      - ./data/storage:/var/www/html/data
      - ./data/custom:/var/www/html/custom
      - ./data/cache:/var/www/html/var
''' + app_port + '''    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - frontend
      - backend
  db:
    image: ${POSTGRES_IMAGE}
    restart: unless-stopped
    environment:
      POSTGRES_USER: fluidweb
      POSTGRES_DB: fluidweb
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - ./data/postgres:/var/lib/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U fluidweb -d fluidweb"]
      interval: 5s
      timeout: 3s
      retries: 15
    networks:
      - backend
  redis:
    image: ${REDIS_IMAGE}
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    volumes:
      - ./data/redis:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 15
    networks:
      - backend
''' + nginx_service + '''networks:
  frontend:
  backend:
    internal: true
'''


NGINX_CONFIG = '''server {
    listen 80 default_server;
    server_name _;
    root /var/www/html/public;
    index index.php;
    client_max_body_size 32m;
    location = /health {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME /var/www/html/public/health.php;
        fastcgi_pass app:9000;
    }
    location / { try_files $uri /index.php$is_args$args; }
    location ~ ^/index\\.php(/|$) {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass app:9000;
        internal;
    }
    location ~ \\.php$ { return 404; }
    location ~ /\\. { deny all; }
}
'''


def check_disk(path: Path) -> None:
    nearest = path
    while not nearest.exists():
        nearest = nearest.parent
    if shutil.disk_usage(nearest).free < 2 * 1024**3:
        raise InstallError('At least 2 GiB of free disk space required for preflight; production sizing may require more.')


def make_layout(path: Path, cfg: dict) -> None:
    path.mkdir(mode=0o755, parents=False, exist_ok=False)
    for rel in ('config', 'secrets', 'data', 'data/postgres', 'data/redis',
                'data/storage', 'data/custom', 'data/cache', 'data/public', 'logs', 'backups'):
        # Host-managed Nginx/Apache must traverse /opt/fluidweb/data/public.
        # All other persistent directories remain private.
        public_dir = rel in ('data', 'data/public')
        (path / rel).mkdir(mode=0o755 if public_dir else 0o700, exist_ok=False)
    safe_json(path / 'settings.json', cfg)
    protected_write(path / '.env', make_env(cfg))
    protected_write(path / 'compose.yaml', render_compose(cfg), mode=0o644)
    protected_write(path / 'config/nginx.conf', NGINX_CONFIG, mode=0o644)
    for f in ('nginx.conf.example', 'apache.conf.example'):
        source = REPO / 'examples' / f
        protected_write(path / 'config' / f, source.read_text().replace('/opt/fluidweb', str(path)).replace(':9080', ':' + str(cfg['fpm_port'] or 9080)), mode=0o644)
    (path / 'src').mkdir(mode=0o700)
    for filename in ('installer.py', 'admin.py', 'fastcgi.py'):
        protected_write(path / 'src' / filename, (REPO / 'src' / filename).read_text())
    control = REPO / 'command.sh'
    protected_write(path / 'command.sh', control.read_text(), mode=0o750)


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


def validate_runtime(path: Path, cfg: dict) -> None:
    compose(path, 'config', '--quiet')
    # Running containers alone are insufficient: probe database, Redis and PHP/FPM.
    for service in ['app', 'db', 'redis'] + (['nginx'] if cfg['web_mode'] == 'nginx' else []):
        if not compose(path, 'ps', '--status', 'running', '-q', service):
            raise InstallError(f'Expected service is not running: {service}')
    compose(path, 'exec', '-T', 'db', 'pg_isready', '-U', 'fluidweb', '-d', 'fluidweb')
    if compose(path, 'exec', '-T', 'redis', 'redis-cli', 'ping') != 'PONG':
        raise InstallError('Redis health probe failed')
    version = compose(path, 'exec', '-T', 'app', 'php', '-r', 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')
    if version != '8.4':
        raise InstallError('Application runtime must be PHP 8.4-FPM')
    compose(path, 'exec', '-T', 'app', 'php', '/opt/installer/health.php')
    if cfg['web_mode'] == 'nginx':
        import urllib.request
        last = None
        for _ in range(20):
            try:
                host=cfg.get('http_bind','0.0.0.0')
                if host=='0.0.0.0': host='127.0.0.1'
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'http://{host}:{cfg["http_port"]}/health', timeout=3) as resp:
                    if resp.status == 200:
                        return
                    last = f'HTTP {resp.status}'
            except Exception as e:
                last = str(e)
            time.sleep(2)
        raise InstallError(f'FluidWeb HTTP health endpoint not available: {last}')
    from fastcgi import probe
    try:
        probe(cfg['fpm_port'])
    except OSError as exc:
        raise InstallError('Loopback FastCGI health failed: '+str(exc)) from exc
    # External host vhost is never modified, so cannot claim external HTTP success.



def operation_lock(path: Path):
    lock_path = path.parent / ('.' + path.name + '.installer.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, 'a+')

def install(args: argparse.Namespace) -> None:
    path = validate_install_dir(args.install_dir)
    validate_host()
    validate_port(args.http_port)
    validate_port(args.fpm_port)
    if args.web_mode == 'external' and args.fpm_port in (80, 443):
        raise InstallError('FPM listener must not use a public HTTP port')
    import ipaddress
    ipaddress.IPv4Address(getattr(args, 'http_bind', '0.0.0.0'))
    if bool(getattr(args, 'tls_cert', None)) != bool(getattr(args, 'tls_key', None)):
        raise InstallError('TLS requires both --tls-cert and --tls-key')
    if getattr(args, 'tls_cert', None) and args.web_mode != 'nginx':
        raise InstallError('External TLS is managed by the operator')
    check_disk(path)
    if os.geteuid() != 0:
        raise InstallError('Use sudo/root to install into the selected directory.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with operation_lock(path) as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('Another FluidWeb installer operation is already running.')
        if path.is_symlink():
            raise InstallError('Refusing a symlinked install root.')
        if path.exists():
            state = path / 'settings.json'
            if not state.exists() or not (path / 'compose.yaml').exists() or not (path / '.env').exists():
                raise InstallError('Unrecognized/partially written installation directory; refusing to overwrite. Preserve files and recover manually.')
            from admin import load
            cfg = load(path)
            if cfg['project'] != project_name(path):
                raise InstallError('Installation identity mismatch; refusing to touch data.')
            installed_file = path / 'installed.json'
            if installed_file.exists():
                try:
                    complete = json.loads(installed_file.read_text())
                except (ValueError, OSError) as exc:
                    raise InstallError('Installation completion marker is corrupt; refusing to overwrite or assume readiness.') from exc
                if not isinstance(complete, dict) or complete.get('state') != 'ready':
                    raise InstallError('Installation completion marker is not ready; manual recovery required.')
                if any((path / name).exists() for name in ('upgrade.json','restore.json')):
                    raise InstallError('Interrupted upgrade: run command.sh recover with explicit approval')
                from admin import load
                load(path)
                if not docker_available():
                    raise InstallError('Docker unavailable: cannot verify existing installation.')
                compose(path, 'up', '-d', '--wait')
                validate_runtime(path, cfg)
                print('Existing installation recovered and health verified; configuration and data preserved.')
                return
            if any((path / name).exists() for name in ('upgrade.json','restore.json')):
                raise InstallError('Interrupted upgrade: run command.sh recover with explicit approval')
            print('Resuming interrupted installation with preserved configuration and credentials.')
            if not docker_available():
                raise InstallError('Docker unavailable: cannot resume existing installation.')
            from admin import load, initialize_public
            load(path)
            initialize_public(path, cfg)
            compose(path, 'up', '-d', '--wait', 'db', 'redis')
            compose(path, 'run', '--rm', '--no-deps', 'app', 'php', '/opt/installer/migrate.php')
            compose(path, 'config', '--quiet')
            compose(path, 'up', '-d', '--wait', timeout=300)
            validate_runtime(path, cfg)
            safe_json(path / 'installed.json', {'installer_version': VERSION, 'state': 'ready', 'created': int(time.time())})
            print('Interrupted install completed without replacing existing data.')
            return
        validate_app_image(args.app_image)
        if not docker_available():
            if not args.install_docker:
                raise InstallError('Working Docker Engine 24+ and Compose 2.20+ required. Re-run with --install-docker to authorize host package installation.')
            sysid, sysver = read_os_release()
            install_docker(sysid, sysver)
        if args.web_mode == 'nginx' and not is_port_free(args.http_port, getattr(args, 'http_bind', '0.0.0.0')):
            raise InstallError(f'HTTP binding {getattr(args, "http_bind", "0.0.0.0")}:{args.http_port} unavailable; choose a free port and local IPv4 address.')
        if args.web_mode == 'external' and not is_port_free(args.fpm_port, '127.0.0.1'):
            raise InstallError(f'Loopback PHP-FPM port {args.fpm_port} is occupied.')
        if getattr(args, 'tls_cert', None):
            validate_port(args.https_port)
            if args.https_port == args.http_port or not is_port_free(args.https_port, args.http_bind):
                raise InstallError('HTTPS port is occupied or equals HTTP port')
        # Pull and pin before touching installation directory.
        images = {'app': image_digest(args.app_image)}
        for key, image in DEFAULT_IMAGES.items():
            images[key] = image_digest(image)
        cfg = settings(args, images)
        make_layout(path, cfg)
        if cfg.get('tls'):
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            (path / 'config/tls').mkdir(mode=0o700)
            protected_write(path / 'config/tls/fullchain.pem', Path(args.tls_cert).read_text())
            protected_write(path / 'config/tls/key.pem', Path(args.tls_key).read_text())
            conf = NGINX_CONFIG.replace('    listen 80 default_server;', '    listen 80 default_server;\n    listen 443 ssl;\n    ssl_certificate /etc/nginx/tls/fullchain.pem;\n    ssl_certificate_key /etc/nginx/tls/key.pem;')
            (path / 'config/nginx.conf').write_text(conf)
        from admin import seal, initialize_public
        seal(path)
        try:
            initialize_public(path, cfg)
            compose(path, 'up', '-d', '--wait', 'db', 'redis')
            compose(path, 'run', '--rm', '--no-deps', 'app', 'php', '/opt/installer/migrate.php')
            compose(path, 'config', '--quiet')
            compose(path, 'up', '-d', '--wait', timeout=300)
            validate_runtime(path, cfg)
        except InstallError:
            # DO NOT remove project state, secrets, volumes or other running apps.
            print(f'Install incomplete, state preserved in {path}. See Docker logs. No cleanup performed.', file=sys.stderr)
            raise
        safe_json(path / 'installed.json', {'installer_version': VERSION, 'state': 'ready', 'created': int(time.time())})
        print(f'Installed at {path}. Mode={cfg["web_mode"]}. Project={cfg["project"]}')
        if cfg['web_mode'] == 'external':
            print(f'Configure the host web server manually; example vhosts: {path}/config/*.conf.example')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='FluidWeb installer v2')
    parser.add_argument('--install-dir', default='/opt/fluidweb')
    parser.add_argument('--web-mode', choices=['nginx', 'external'], default=None)
    parser.add_argument('--http-port', type=int, default=80)
    parser.add_argument('--fpm-port', type=int, default=9080)
    parser.add_argument('--app-image', default='', help='Required FluidWeb release digest ref')
    parser.add_argument('--install-docker', action='store_true', help='Explicitly authorize Docker package installation')
    parser.add_argument('--http-bind', default='0.0.0.0')
    parser.add_argument('--https-port', type=int, default=443)
    parser.add_argument('--tls-cert', help='PEM certificate chain, with --tls-key')
    parser.add_argument('--tls-key', help='PEM private key')
    parser.add_argument('--preflight', action='store_true', help='Inspect host without changing anything')
    args = parser.parse_args(argv)
    try:
        if args.preflight:
            validate_host()
            validate_install_dir(args.install_dir)
            check_disk(Path(args.install_dir))
            print(json.dumps({'os': read_os_release(), 'docker_operational': docker_available(),
                              'http_port_free': is_port_free(args.http_port), 'fpm_port_free': is_port_free(args.fpm_port)}, indent=2))
            return 0
        if args.web_mode is None:
            if not sys.stdin.isatty():
                args.web_mode = 'nginx'
            else:
                print('Web server: [1] Nginx in Docker (default), [2] host-managed web server')
                choice = input('Choose 1 or 2 [1]: ').strip()
                if choice not in ('', '1', '2'):
                    raise InstallError('Unsupported web server choice.')
                args.web_mode = 'external' if choice == '2' else 'nginx'
        install(args)
        return 0
    except (InstallError, OSError, ValueError) as exc:
        print(f'INSTALLER ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
