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

VERSION = '0.1.0'
SUPPORTED = {'ubuntu': {'22.04', '24.04'}, 'debian': {'12', '13'}}
DEFAULT_IMAGES = {'postgres': 'postgres:18-alpine', 'redis': 'redis:8-alpine', 'nginx': 'nginx:1.28-alpine'}
REPO = Path(__file__).resolve().parents[1]


class InstallError(Exception):
    pass


def run(cmd: list[str], *, timeout: int = 120, capture: bool = True) -> str:
    try:
        proc = subprocess.run(cmd, check=True, text=True,
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
    if path.is_symlink():
        raise InstallError('Install directory must not be a symlink')
    return path


def validate_port(port: int) -> None:
    if not 1024 <= port <= 65535 and port not in {80, 443}:
        raise InstallError('Port must be 80, 443 or between 1024 and 65535')


def is_port_free(port: int, host: str = '0.0.0.0') -> bool:
    # Binding is not a reservation; Docker still performs final collision check.
    for family, addr in [(socket.AF_INET, ('0.0.0.0', port)), (socket.AF_INET6, ('::', port))]:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind(addr)
        except OSError as exc:
            if exc.errno in (97, 99):  # IPv6 not supported
                continue
            return False
    return True


def validate_app_image(image: str) -> None:
    # Never deploy an unversioned/development image to an end user's server.
    if not re.fullmatch(r'[a-zA-Z0-9._/-]+@sha256:[a-fA-F0-9]{64}', image):
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
        run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=15)
        run(['docker', 'compose', 'version', '--short'], timeout=15)
        return True
    except InstallError:
        return False


def install_docker(system: str, release: str) -> None:
    """Install from Docker's official apt repository only when explicitly authorized."""
    if shutil.which('docker'):
        raise InstallError('Docker binary exists but the daemon/Compose is unavailable. Refusing to replace it.')
    if not shutil.which('apt-get') or not shutil.which('curl'):
        raise InstallError('apt-get and curl are required to bootstrap Docker. Install curl first.')
    run(['apt-get', 'update'], timeout=300, capture=False)
    run(['apt-get', 'install', '-y', '--no-install-recommends', 'ca-certificates', 'curl', 'gnupg'], timeout=300, capture=False)
    # No curl-to-shell. Download Docker repository signing key and validate via gpg.
    keyring = Path('/etc/apt/keyrings')
    keyring.mkdir(parents=True, exist_ok=True)
    keyfile = keyring / 'docker.asc'
    host = 'ubuntu' if system == 'ubuntu' else 'debian'
    run(['curl', '--proto', '=https', '--tlsv1.2', '-fsSL',
         f'https://download.docker.com/linux/{host}/gpg', '-o', str(keyfile)], timeout=60)
    # Docker's published signing key fingerprint. Fail closed if the download is replaced.
    fingerprint = run(['gpg', '--show-keys', '--with-colons', '--fingerprint', str(keyfile)])
    if '9DC858229FC7DD38854AE2D88D81803C0EBFCD88' not in fingerprint.replace(':', '').upper():
        raise InstallError('Docker repository signing key fingerprint did not match expected key')
    os.chmod(keyfile, 0o644)
    arch = run(['dpkg', '--print-architecture'])
    distro = {'ubuntu': {'22.04': 'jammy', '24.04': 'noble'},
              'debian': {'12': 'bookworm', '13': 'trixie'}}[system][release]
    source = Path('/etc/apt/sources.list.d/docker-fluidweb.sources')
    if source.exists():
        raise InstallError(f'Existing Docker apt source {source}; review manually before continuing.')
    source.write_text(f'Types: deb\nURIs: https://download.docker.com/linux/{host}\nSuites: {distro}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {keyfile}\n')
    run(['apt-get', 'update'], timeout=300, capture=False)
    run(['apt-get', 'install', '-y', '--no-install-recommends',
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


def safe_json(path: Path, obj: dict) -> None:
    protected_write(path, json.dumps(obj, indent=2, sort_keys=True) + '\n')


def settings(args: argparse.Namespace, resolved: dict[str, str]) -> dict:
    return {'version': 1, 'web_mode': args.web_mode,
            'http_port': args.http_port if args.web_mode == 'nginx' else None,
            'fpm_port': args.fpm_port if args.web_mode == 'external' else None,
            'project': project_name(Path(args.install_dir)),
            'images': resolved}


def make_env(cfg: dict) -> str:
    lines = {'COMPOSE_PROJECT_NAME': cfg['project'],
             'APP_IMAGE': cfg['images']['app'],
             'POSTGRES_IMAGE': cfg['images']['postgres'],
             'REDIS_IMAGE': cfg['images']['redis'],
             'NGINX_IMAGE': cfg['images']['nginx'],
             'POSTGRES_PASSWORD': secrets.token_hex(32),
             'APP_SECRET': secrets.token_hex(32),
             'HTTP_PORT': str(cfg['http_port'] or 8080),
             'FPM_PORT': str(cfg['fpm_port'] or 9080)}
    return ''.join(f'{k}={v}\n' for k, v in lines.items())


def render_compose(cfg: dict) -> str:
    # Files/images are local-to-install-root, no absolute host resource collisions.
    nginx = cfg['web_mode'] == 'nginx'
    app_port = '' if nginx else '    ports:\n      - "127.0.0.1:${FPM_PORT}:9000"\n'
    nginx_service = '''  nginx:
    image: ${NGINX_IMAGE}
    restart: unless-stopped
    ports:
      - "${HTTP_PORT}:80"
    volumes:
      - ./data/public:/var/www/html/public:ro
      - ./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - app
    networks:
      - frontend
''' if nginx else ''
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
        protected_write(path / 'config' / f, source.read_text(), mode=0o644)
    control = REPO / 'command.sh'
    protected_write(path / 'command.sh', control.read_text(), mode=0o750)


def compose(path: Path, *args: str, timeout: int = 300) -> str:
    return run(['docker', 'compose', '--project-directory', str(path), '--env-file', str(path / '.env'),
                '-f', str(path / 'compose.yaml'), *args], timeout=timeout)


def validate_runtime(path: Path, cfg: dict) -> None:
    compose(path, 'config', '--quiet')
    # All services must report running, and Symfony must bootstrap successfully.
    for service in ['app', 'db', 'redis'] + (['nginx'] if cfg['web_mode'] == 'nginx' else []):
        if not compose(path, 'ps', '--status', 'running', '-q', service):
            raise InstallError(f'Expected service is not running: {service}')
    compose(path, 'exec', '-T', 'app', 'php', 'bin/console', 'list', '--no-interaction')
    if cfg['web_mode'] == 'nginx':
        import urllib.request
        last = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{cfg["http_port"]}/health', timeout=3) as resp:
                    if resp.status == 200:
                        return
                    last = f'HTTP {resp.status}'
            except Exception as e:
                last = str(e)
            time.sleep(2)
        raise InstallError(f'FluidWeb HTTP health endpoint not available: {last}')
    # External host vhost is never modified, so cannot claim external HTTP success.


def install(args: argparse.Namespace) -> None:
    path = validate_install_dir(args.install_dir)
    validate_host()
    validate_port(args.http_port)
    validate_port(args.fpm_port)
    if args.web_mode == 'external' and args.fpm_port in (80, 443):
        raise InstallError('FPM listener must not use a public HTTP port')
    check_disk(path)
    if os.geteuid() != 0:
        raise InstallError('Use sudo/root to install into the selected directory.')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ('.' + path.name + '.installer.lock')
    with lock_path.open('a+') as lock:
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
            cfg = json.loads(state.read_text())
            if cfg['project'] != project_name(path):
                raise InstallError('Installation identity mismatch; refusing to touch data.')
            installed_file = path / 'installed.json'
            if installed_file.exists():
                try:
                    complete = json.loads(installed_file.read_text())
                except (ValueError, OSError) as exc:
                    raise InstallError('Installation completion marker is corrupt; refusing to overwrite or assume readiness.') from exc
                if complete.get('state') != 'ready':
                    raise InstallError('Installation completion marker is not ready; manual recovery required.')
                print('Existing installation found. No configuration or data changed.')
                return
            print('Resuming interrupted installation with preserved configuration and credentials.')
            if not docker_available():
                raise InstallError('Docker unavailable: cannot resume existing installation.')
            compose(path, 'config', '--quiet')
            compose(path, 'up', '-d', '--wait', timeout=300)
            validate_runtime(path, cfg)
            safe_json(path / 'installed.json', {'installer_version': VERSION, 'state': 'ready', 'created': int(time.time())})
            print('Interrupted install completed without replacing existing data.')
            return
        validate_app_image(args.app_image)
        if not docker_available():
            if not args.install_docker:
                raise InstallError('Working Docker Engine + Compose plugin required. Re-run with --install-docker to authorize host package installation.')
            sysid, sysver = read_os_release()
            install_docker(sysid, sysver)
        if args.web_mode == 'nginx' and not is_port_free(args.http_port):
            raise InstallError(f'HTTP port {args.http_port} is occupied. Choose another --http-port or external mode.')
        if args.web_mode == 'external' and not is_port_free(args.fpm_port):
            raise InstallError(f'Loopback PHP-FPM port {args.fpm_port} is occupied.')
        # Pull and pin before touching installation directory.
        images = {'app': image_digest(args.app_image)}
        for key, image in DEFAULT_IMAGES.items():
            images[key] = image_digest(image)
        cfg = settings(args, images)
        make_layout(path, cfg)
        try:
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
                raise InstallError('Noninteractive installation requires --web-mode nginx or --web-mode external.')
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
