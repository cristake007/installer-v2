"""Nginx and certificates for this installation; no host web-server changes."""
import copy
import json
import http.client
import ipaddress
from pathlib import Path
import re
import socket
import ssl
import tempfile

import installer as i

CERTBOT_IMAGE='certbot/certbot:v5.8.0'
ACME_SERVER='https://acme-v02.api.letsencrypt.org/directory'
PHP_INI='''expose_php = Off
display_errors = Off
log_errors = On
memory_limit = 256M
upload_max_filesize = 32M
post_max_size = 32M
date.timezone = UTC
'''
FPM_CONFIG='''[www]
pm = dynamic
pm.max_children = 10
pm.start_servers = 2
pm.min_spare_servers = 1
pm.max_spare_servers = 3
'''
RENEW_SCRIPT='''#!/bin/sh
trap 'exit 0' TERM INT
while :; do
    certbot renew --non-interactive --quiet || echo "Certificate renewal failed; inspect certbot logs" >&2
    sleep 43200 &
    wait $!
done
'''


def domain(value,acme=False):
    if not value or len(value)>253 or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',value):
        raise i.InstallError('Enter a valid hostname or IPv4 address')
    if any(not label or len(label)>63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
        raise i.InstallError('Invalid hostname labels')
    if acme:
        try: ipaddress.ip_address(value)
        except ValueError: pass
        else: raise i.InstallError('Let\'s Encrypt mode requires a DNS hostname')
        if '.' not in value: raise i.InstallError('Let\'s Encrypt requires a fully qualified domain')
    return value.lower()


def validate(cfg):
    mode=cfg.get('tls_mode','http')
    if mode not in ('http','own','letsencrypt'): raise i.InstallError('Invalid HTTPS mode')
    if cfg.get('domain'): domain(cfg['domain'],mode=='letsencrypt')
    if mode!='http':
        if cfg['web_mode']!='nginx': raise i.InstallError('Container HTTPS requires Nginx mode; external TLS belongs to your web-server operator')
        if not cfg.get('domain'): raise i.InstallError('HTTPS requires --domain')
        i.validate_port(cfg['https_port'])
        if cfg['https_port']==cfg['http_port']: raise i.InstallError('HTTP and HTTPS require different ports')
    if mode=='letsencrypt':
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',cfg.get('email','')): raise i.InstallError('A valid email is required for Let\'s Encrypt')
        if cfg.get('acme_server',ACME_SERVER)==ACME_SERVER and cfg['http_port']!=80:
            raise i.InstallError('Public Let\'s Encrypt HTTP-01 requires port 80; choose it or use your external web server')
        if not cfg.get('acme_server',ACME_SERVER).startswith('https://'): raise i.InstallError('ACME server must use HTTPS')
        pin=cfg.get('certbot_image','')
        if pin and not re.fullmatch(r'(?:docker.io/)?certbot/certbot@sha256:[a-f0-9]{64}',pin): raise i.InstallError('Invalid pinned Certbot image')


def certificate_paths(path,cfg):
    if cfg.get('tls_mode')=='letsencrypt':
        if (path/'config/letsencrypt').is_symlink() or (path/'config/letsencrypt/archive').is_symlink():
            raise i.InstallError('Unsafe certificate directory')
        base=path/'config/letsencrypt/live'/cfg['domain']
        for name in ('fullchain.pem','privkey.pem'):
            target=base/name
            if target.exists() and not target.resolve().is_relative_to((path/'config/letsencrypt/archive').resolve()):
                raise i.InstallError('Certificate link escapes the installation archive directory')
        return base/'fullchain.pem',base/'privkey.pem'
    return path/'config/tls/fullchain.pem',path/'config/tls/key.pem'


def nginx(cfg,tls=None):
    enabled=cfg.get('tls_mode','http')!='http' if tls is None else tls
    text=i.NGINX_CONFIG.replace('server_name _;', 'server_name '+cfg.get('domain','_')+';')
    if cfg.get('tls_mode')=='letsencrypt':
        text=text.replace('    location / {','    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }\n    location / {')
    if enabled:
        if cfg['tls_mode']=='letsencrypt':
            base='/etc/letsencrypt/live/'+cfg['domain'];cert=base+'/fullchain.pem';key=base+'/privkey.pem'
        else: cert='/etc/nginx/tls/fullchain.pem';key='/etc/nginx/tls/key.pem'
        text=text.replace('    listen 80 default_server;',f'    listen 443 ssl;\n    ssl_certificate {cert};\n    ssl_certificate_key {key};\n    ssl_protocols TLSv1.2 TLSv1.3;')
        canonical='https://'+cfg['domain']+((':'+str(cfg['https_port'])) if cfg['https_port']!=443 else '')
        challenge='    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }\n' if cfg['tls_mode']=='letsencrypt' else ''
        text='server {\n    listen 80 default_server;\n    server_name '+cfg['domain']+';\n'+challenge+'''    location = /health {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME /var/www/html/public/health.php;
        fastcgi_pass php:9000;
    }
'''+f'    location / {{ return 301 {canonical}$request_uri; }}\n}}\n'+text
    return text


def initialize(path,cfg):
    for name,content in (('php.ini',PHP_INI),('php-fpm.conf',FPM_CONFIG),('renew.sh',RENEW_SCRIPT)):
        target=path/'config'/name
        if not target.exists(): i.protected_write(target,content,0o644)
    for name in ('tls','acme','letsencrypt','.last-good'):
        target=path/'config'/name
        if target.is_symlink():raise i.InstallError('Unsafe configuration directory')
        target.mkdir(mode=0o700,exist_ok=True)
    # Only the public HTTP-01 challenge directory is readable by Nginx workers.
    (path/'config/acme').chmod(0o755)
    if cfg.get('tls_mode')=='letsencrypt':
        challenge=path/'config/acme/.well-known/acme-challenge'
        challenge.mkdir(parents=True,exist_ok=True)
        for target in (challenge,challenge.parent):target.chmod(0o755)


def save_good(path):
    import admin
    for name in ('nginx.conf','php.ini','php-fpm.conf'):
        admin.atomic(path/'config/.last-good'/name,(path/'config'/name).read_text())


def use_own_certificate(path,cfg,cert,key):
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert),str(key))
    import admin
    admin.atomic(path/'config/tls/fullchain.pem',Path(cert).read_text())
    admin.atomic(path/'config/tls/key.pem',Path(key).read_text())


def certbot(path,cfg,*arguments):
    mounts=['-v',str(path/'config/letsencrypt')+':/etc/letsencrypt',
            '-v',str(path/'config/acme')+':/var/www/certbot']
    if cfg.get('acme_ca'):
        mounts+=['-v',str(path/'config/acme-ca.pem')+':/acme-ca.pem:ro','-e','REQUESTS_CA_BUNDLE=/acme-ca.pem']
    # No listener or Docker socket: host networking is only for outbound CA access.
    actual=json.loads((path/'settings.json').read_text())
    scheduled=actual.get('tls_mode')=='letsencrypt' and actual.get('auto_renew')
    if scheduled:stop_renewal(path,actual)
    with tempfile.TemporaryDirectory(prefix='.certbot-job-',dir=path) as temporary:
        cidfile=Path(temporary)/'container-id'
        try:
            i.run(['docker','run','--rm','--cidfile',str(cidfile),'--label','com.docker.compose.project='+cfg['project'],
                   '--network','host',*mounts,cfg['certbot_image'],*arguments],timeout=300,capture=False)
        finally:
            if cidfile.exists():
                cid=cidfile.read_text().strip()
                if re.fullmatch('[a-f0-9]{64}',cid):
                    try:i.run(['docker','container','inspect',cid])
                    except i.InstallError:pass  # --rm already removed the completed job.
                    else:i.run(['docker','rm','-f',cid])
            if scheduled:i.compose(path,'up','-d','--wait','certbot')



def generate(path,cfg):
    if cfg.get('tls_mode')!='letsencrypt':raise i.InstallError('This installation is not configured for Let\'s Encrypt')
    certbot(path,cfg,'certonly','--webroot','-w','/var/www/certbot','--non-interactive','--agree-tos',
            '--email',cfg['email'],'--cert-name',cfg['domain'],'-d',cfg['domain'],
            '--server',cfg.get('acme_server',ACME_SERVER),'--keep-until-expiring')
    cert,key=certificate_paths(path,cfg)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert),str(key))


def renew(path,cfg,force=False):
    if cfg.get('tls_mode')!='letsencrypt':raise i.InstallError('Certificate renewal requires Let\'s Encrypt mode')
    certbot(path,cfg,'renew','--non-interactive','--no-random-sleep-on-renew',*(['--force-renewal'] if force else []))
    i.compose(path,'exec','-T','nginx','nginx','-t');i.compose(path,'exec','-T','nginx','nginx','-s','reload')
    https_health(path,cfg)


def https_health(path,cfg):
    cert,_=certificate_paths(path,cfg)
    context=ssl.create_default_context()
    context.load_verify_locations(str(cert))
    # Trust the installed leaf/chain for the local probe, including private/own CAs.
    context.verify_flags |= getattr(ssl,'VERIFY_X509_PARTIAL_CHAIN',0x80000)
    host=cfg['http_bind'] if cfg['http_bind']!='0.0.0.0' else '127.0.0.1'
    connection=http.client.HTTPConnection(host,cfg['https_port'],timeout=5)
    connection.sock=context.wrap_socket(socket.create_connection((host,cfg['https_port']),timeout=5),server_hostname=cfg['domain'])
    try:
        connection.request('GET','/health',headers={'Host':cfg['domain']})
        response=connection.getresponse()
        if response.status!=200 or response.read().strip()!=b'PHP 8.4 OK':raise i.InstallError('HTTPS PHP health failed')
    finally:connection.close()


def apply(path,cfg,cert=None,key=None):
    """Apply a new domain/TLS configuration with a retained cold safety backup."""
    import admin
    old=i.load(path);validate(cfg)
    for port in (cfg['http_port'],*([cfg['https_port']] if cfg.get('tls_mode','http')!='http' else [])):
        already=old['web_mode']=='nginx' and port in (old['http_port'],old.get('https_port') if old.get('tls_mode','http')!='http' else None)
        if not already and not i.is_port_free(port,cfg['http_bind']):raise i.InstallError(f'Port {port} is occupied')
    if cfg.get('tls_mode')=='own':
        if not cert or not key:cert,key=certificate_paths(path,old)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert),str(key))
    if cfg.get('tls_mode')=='letsencrypt' and not cfg.get('certbot_image'):
        cfg['certbot_image']=i.image_digest(CERTBOT_IMAGE)
    saved=admin.backup(path,old)
    stop_renewal(path,old)
    try:
        initialize(path,cfg)
        if cfg.get('tls_mode')=='own':use_own_certificate(path,cfg,cert,key)
        # Serve the ACME challenge over HTTP before enabling HTTPS.
        if cfg.get('tls_mode')=='letsencrypt':
            pending=copy.deepcopy(cfg);pending['auto_renew']=False
            admin.atomic(path/'settings.json',json.dumps(pending,indent=2)+'\n')
            admin.atomic(path/'compose.yaml',i.render_compose(pending))
            admin.atomic(path/'config/nginx.conf',nginx(cfg,tls=False))
            i.compose(path,'up','-d','--wait','--force-recreate');generate(path,cfg)
        admin.atomic(path/'settings.json',json.dumps(cfg,indent=2)+'\n')
        admin.atomic(path/'compose.yaml',i.render_compose(cfg))
        admin.atomic(path/'config/nginx.conf',nginx(cfg))
        i.compose(path,'up','-d','--wait','--force-recreate');i.validate_runtime(path,cfg);save_good(path)
    except Exception:
        admin.restore(path,saved,old,take_safety=False)
        raise i.InstallError('Web configuration failed; previous configuration and data restored')


def stop_renewal(path,cfg):
    if cfg.get('tls_mode')=='letsencrypt' and cfg.get('auto_renew'):
        cid=i.compose(path,'ps','--all','-q','certbot')
        if cid:i.run(['docker','rm','-f',cid])
