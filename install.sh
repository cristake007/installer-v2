#!/usr/bin/env bash
# Installer v2 - Bash and Docker Compose. Nginx mode only.
set -euo pipefail

DIR='/opt/fluidweb'
HTTP_PORT=80
SSL_ENABLED=0
HTTP_TO_SSL=0
DOMAIN=''
MENU_CHOICE=0
TTY=0
[[ -t 0 && -t 1 && ${TERM:-dumb} != dumb ]] && TTY=1
if (( TTY )); then
  BLUE=$'\033[1;34m'; CYAN=$'\033[1;36m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BLUE=''; CYAN=''; DIM=''; RESET=''
fi

cleanup_terminal() { (( TTY )) && printf '\033[?25h\033[0m' || true; }
trap cleanup_terminal EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

header() {
  (( TTY )) && printf '\033[2J\033[H\033[?25l' || printf '\n'
  printf '%s  INSTALLER V2%s  %sDocker infrastructure%s\n' "$BLUE" "$RESET" "$DIM" "$RESET"
  printf '  --------------------------------------------------------\n'
  printf '\n  %s\n\n' "$1"
}

message() { printf '\n  %s\n' "$*"; }
fail() { printf '\n  ERROR: %s\n' "$*" >&2; exit 1; }

# Sets MENU_CHOICE (zero-based); Esc or EOF cancels.
menu() {
  local title=$1 caption=$2 key seq i selected=0 n
  shift 2
  local -a choices=("$@")
  while :; do
    header "$title"
    printf '  %s\n\n' "$caption"
    for ((i=0;i<${#choices[@]};i++)); do
      if (( TTY && i==selected )); then
        printf '  %s> %s%s\n' "$CYAN" "${choices[i]}" "$RESET"
      else
        printf '    %s\n' "${choices[i]}"
      fi
    done
    printf '\n  %sArrows: navigate  Enter: select  Esc: back%s\n' "$DIM" "$RESET"
    if (( ! TTY )); then
      printf '  Choose [1-%s, q=back]: ' "${#choices[@]}"
      IFS= read -r key || return 1
      [[ $key == q || $key == Q ]] && return 1
      if [[ $key =~ ^[1-9]$ ]] && (( 10#$key <= ${#choices[@]} )); then
        MENU_CHOICE=$((10#$key-1)); return 0
      fi
      continue
    fi
    IFS= read -rsn1 key || return 1
    case "$key" in
      $'\033')
        seq=''; IFS= read -rsn2 -t 0.12 seq || true
        case "$seq" in
          '[A') selected=$(((selected+${#choices[@]}-1)%${#choices[@]}));;
          '[B') selected=$(((selected+1)%${#choices[@]}));;
          '') return 1;;
        esac;;
      '') MENU_CHOICE=$selected; return 0;;
      [1-9]) n=$((key-1)); if (( n < ${#choices[@]} )); then MENU_CHOICE=$n; return 0; fi;;
    esac
  done
}

# Arrow navigation + Space to toggle checkbox. No whiptail required.
nginx_options() {
  local selected=0 key seq
  while :; do
    header 'NGINX OPTIONS'
    printf '  Select optional features:\n\n'
    if (( selected==0 && TTY )); then printf '  %s> %s Enable SSL (free Let\x27s Encrypt certificate)%s\n' "$CYAN" "$([[ $SSL_ENABLED == 1 ]] && echo '[x]' || echo '[ ]')" "$RESET"
    else printf '    %s Enable SSL (free Let\x27s Encrypt certificate)\n' "$([[ $SSL_ENABLED == 1 ]] && echo '[x]' || echo '[ ]')"; fi
    if (( selected==1 && TTY )); then printf '  %s> Continue%s\n' "$CYAN" "$RESET"; else printf '    Continue\n'; fi
    if (( selected==2 && TTY )); then printf '  %s> Back%s\n' "$CYAN" "$RESET"; else printf '    Back\n'; fi
    printf '\n  %sArrows: navigate  Space: toggle  Enter: select  Esc: back%s\n' "$DIM" "$RESET"
    if (( ! TTY )); then
      printf '  Enable SSL? [y/N, q=back]: '
      IFS= read -r key || return 1
      case "$key" in y|Y|yes|YES) SSL_ENABLED=1;return 0;; n|N|no|NO|'') SSL_ENABLED=0;return 0;; q|Q) return 1;; esac
      continue
    fi
    IFS= read -rsn1 key || return 1
    case "$key" in
      $'\033')
        seq=''; IFS= read -rsn2 -t 0.12 seq || true
        case "$seq" in
          '[A') selected=$(((selected+2)%3));;
          '[B') selected=$(((selected+1)%3));;
          '') return 1;;
        esac;;
      ' ') if (( selected==0 )); then SSL_ENABLED=$((1-SSL_ENABLED)); fi;;
      '') case "$selected" in 0) SSL_ENABLED=$((1-SSL_ENABLED));; 1) return 0;; 2) return 1;; esac;;
    esac
  done
}

ask_domain() {
  local error=''
  while :; do
    header 'SSL DOMAIN'
    [[ -z $error ]] || printf '  ERROR: %s\n\n' "$error"
    printf '  Domain pointing to this server (example: app.example.com):\n'
    printf '  Enter a domain to retry, or q / Esc then Enter to go back.\n  > '
    IFS= read -r DOMAIN || return 1
    DOMAIN=${DOMAIN,,}
    case "$DOMAIN" in q|$'\033') return 1;; esac
    if [[ ! $DOMAIN =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
      error='Invalid domain.'
    elif ! getent ahostsv4 "$DOMAIN" >/dev/null; then
      error="The domain $DOMAIN has no resolvable IPv4 address. Configure DNS first."
    else
      return 0
    fi
  done
}

ask_dir() {
  header 'INSTALL DIRECTORY'
  printf '  Installation directory [/opt/fluidweb]: '
  local value
  IFS= read -r value || return 1
  DIR=${value:-/opt/fluidweb}
  [[ $DIR == /* && $DIR != / && $DIR != /opt && $DIR != /srv ]] || { message 'Use a full directory path (not /).'; return 1; }
  [[ $DIR != *$'\n'* ]] || return 1
  DIR=${DIR%/}
}

confirm() {
  if menu 'CONFIRM INSTALLATION' "Location: $DIR | Nginx: public 0.0.0.0:$HTTP_PORT | SSL: $([[ $SSL_ENABLED == 1 ]] && printf '%s' "$DOMAIN (public 443)" || printf 'off')" \
      'Install / verify services' 'Back'; then
    (( MENU_CHOICE==0 ))
  else
    return 1
  fi
}

preflight() {
  [[ $EUID == 0 ]] || fail 'Run with sudo: sudo bash install.sh'
  [[ -f /etc/os-release ]] || fail 'Cannot determine operating system.'
  # shellcheck disable=SC1091
  . /etc/os-release
  case "$ID" in ubuntu|debian) :;; *) fail 'Supported systems: Ubuntu and Debian.';; esac
  for tool in openssl ss; do command -v "$tool" >/dev/null || fail "Missing $tool (install openssl/iproute2)."; done
  if (( SSL_ENABLED )); then
    command -v getent >/dev/null || fail 'getent is required for DNS checks.'
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    if menu 'DOCKER IS REQUIRED' 'Docker Engine + Compose were not found.' 'Install Docker automatically' 'Cancel'; then
      (( MENU_CHOICE==0 )) || return 1
    else return 1; fi
    if ! command -v curl >/dev/null; then
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl
    fi
    local tmp
    tmp=$(mktemp)
    curl -fsSL https://get.docker.com -o "$tmp" || { rm -f "$tmp"; fail 'Could not download Docker installer.'; }
    sh "$tmp" || { rm -f "$tmp"; fail 'Docker installation failed.'; }
    rm -f "$tmp"
  fi
  docker info >/dev/null 2>&1 || fail 'Docker daemon is not running or accessible.'
  docker compose version >/dev/null 2>&1 || fail 'Docker Compose plugin not available.'
  if ! command -v curl >/dev/null; then
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates
  fi
}

port_busy() { [[ -n $(ss -H -ltn "sport = :$1") ]]; }

validate_dir_and_ports() {
  HTTP_TO_SSL=0
  if [[ -e "$DIR" && ! -d "$DIR" ]]; then fail "$DIR exists but is not a directory."; fi
  if [[ -L "$DIR" ]]; then fail 'Installation path must not be a symlink.'; fi
  if [[ -f "$DIR/.installer-settings" ]]; then
    [[ -f "$DIR/compose.yaml" && -f "$DIR/.env" ]] || fail 'Existing installation is incomplete. No files were overwritten.'
    ensure_postgres_volume
    # Compare literal settings; never execute configuration content as root.
    if grep -Fxq "SAVED_HTTP_PORT=$HTTP_PORT" "$DIR/.installer-settings" &&
       grep -Fxq "SAVED_SSL=$SSL_ENABLED" "$DIR/.installer-settings" &&
       grep -Fxq "SAVED_DOMAIN=$DOMAIN" "$DIR/.installer-settings"; then
      message 'Existing installation found. Services will be verified without replacing data or passwords.'
    elif (( SSL_ENABLED )) && (( HTTP_PORT == 80 )) &&
         grep -Fxq 'SAVED_HTTP_PORT=80' "$DIR/.installer-settings" &&
         grep -Fxq 'SAVED_SSL=0' "$DIR/.installer-settings" &&
         grep -Fxq 'SAVED_DOMAIN=' "$DIR/.installer-settings"; then
      port_busy 443 && fail 'Public HTTPS port 443 is already in use.'
      HTTP_TO_SSL=1
      message 'Existing HTTP installation found. SSL will be enabled without replacing data or passwords.'
    else
      fail 'Existing installation uses different options. Data and settings were not changed.'
    fi
  else
    if [[ -d "$DIR" && -n $(find "$DIR" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
      fail "$DIR is not empty and is not an installer-v2 installation."
    fi
    if port_busy "$HTTP_PORT"; then fail "Public port $HTTP_PORT is already in use. Free it before installing."; fi
    if (( SSL_ENABLED )) && port_busy 443; then fail 'Public HTTPS port 443 is already in use.'; fi
  fi
}

# Enable HTTPS for an existing HTTP installation on public port 80.
# Update only the Nginx port line and stored options. Keep all data and secrets.
enable_ssl_on_existing_http() {
  local port_line="      - '0.0.0.0:\${HTTP_PORT}:80'"
  grep -Fxq "$port_line" "$DIR/compose.yaml" || fail 'Unexpected Nginx port configuration; no changes made.'
  grep -Fxq "      - '0.0.0.0:443:443'" "$DIR/compose.yaml" && fail 'HTTPS port already configured unexpectedly.'
  sed -i "/HTTP_PORT}:80'$/a\      - '0.0.0.0:443:443'" "$DIR/compose.yaml"
  grep -Fxq "      - '0.0.0.0:443:443'" "$DIR/compose.yaml" || fail 'Could not enable the HTTPS port.'
  printf 'SAVED_HTTP_PORT=80\nSAVED_SSL=1\nSAVED_DOMAIN=%s\n' "$DOMAIN" > "$DIR/.installer-settings"
  chmod 600 "$DIR/.installer-settings"
}

dc() { docker compose --project-directory "$DIR" --env-file "$DIR/.env" -f "$DIR/compose.yaml" "$@"; }

# Leave legacy PostgreSQL storage untouched, even when empty.
ensure_postgres_volume() {
  if grep -Fxq '      - ./data/postgres:/var/lib/postgresql' "$DIR/compose.yaml"; then
    fail 'Existing installation uses a legacy PostgreSQL bind mount. No automatic migration was performed; data and settings are untouched.'
  fi
}

write_new_installation() {
  umask 077
  mkdir -p "$DIR"/{config,public,data/redis,certbot/conf,certbot/www}
  chmod 700 "$DIR" "$DIR/config" "$DIR/data" "$DIR/certbot/conf"
  chmod 755 "$DIR/public" "$DIR/certbot" "$DIR/certbot/www"
  local password project
  password=$(openssl rand -hex 24)
  project="fluidweb_$(openssl rand -hex 5)"
  printf 'COMPOSE_PROJECT_NAME=%s\nPOSTGRES_PASSWORD=%s\nHTTP_PORT=%s\n' "$project" "$password" "$HTTP_PORT" > "$DIR/.env"
  printf 'SAVED_HTTP_PORT=%s\nSAVED_SSL=%s\nSAVED_DOMAIN=%s\n' "$HTTP_PORT" "$SSL_ENABLED" "$DOMAIN" > "$DIR/.installer-settings"
  cat > "$DIR/public/index.php" <<'PHP'
<?php
header('Content-Type: text/plain; charset=utf-8');
echo "Installer v2: PHP-FPM OK\n";
PHP
  chmod 644 "$DIR/public/index.php"
  cat > "$DIR/config/nginx.conf" <<'NGINX'
server {
    listen 80;
    server_name _;
    root /var/www/html/public;
    index index.php;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { try_files $uri $uri/ /index.php?$query_string; }
    location ~ \.php$ {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME /var/www/html/public$fastcgi_script_name;
        fastcgi_pass php:9000;
    }
}
NGINX
  local https_port=''
  (( SSL_ENABLED )) && https_port="      - '0.0.0.0:443:443'"
  cat > "$DIR/compose.yaml" <<YAML
services:
  postgres:
    image: postgres:18
    restart: unless-stopped
    environment:
      POSTGRES_USER: installer
      POSTGRES_DB: installer
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql
    healthcheck:
      test: ['CMD-SHELL', 'pg_isready -U installer -d installer']
      interval: 5s
      timeout: 3s
      retries: 20
  redis:
    image: redis:8
    restart: unless-stopped
    command: ['redis-server', '--appendonly', 'yes']
    volumes:
      - ./data/redis:/data
    healthcheck:
      test: ['CMD', 'redis-cli', 'ping']
      interval: 5s
      timeout: 3s
      retries: 20
  php:
    image: php:8.4-fpm
    restart: unless-stopped
    volumes:
      - ./public:/var/www/html/public:ro
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
  nginx:
    image: nginx:stable
    restart: unless-stopped
    ports:
      - '0.0.0.0:\${HTTP_PORT}:80'
$https_port
    volumes:
      - ./public:/var/www/html/public:ro
      - ./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certbot/www:/var/www/certbot:ro
      - ./certbot/conf:/etc/letsencrypt:ro
    depends_on:
      - php
  certbot:
    image: certbot/certbot:latest
    profiles: ['tools']
    volumes:
      - ./certbot/www:/var/www/certbot
      - ./certbot/conf:/etc/letsencrypt

volumes:
  postgres_data:
YAML
  cp "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/command.sh" "$DIR/command.sh"
  chmod 700 "$DIR/command.sh"
  chmod 600 "$DIR/.env" "$DIR/.installer-settings" "$DIR/compose.yaml" "$DIR/config/nginx.conf"
}

wait_http() {
  local url=$1 expected=$2 i body
  for ((i=0;i<40;i++)); do
    body=$(curl -fsS --connect-timeout 2 --max-time 4 "$url" 2>/dev/null) || body=''
    [[ $body == *"$expected"* ]] && return 0
    sleep 2
  done
  return 1
}

https_config() {
  cat > "$DIR/config/nginx.conf" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    root /var/www/html/public;
    index index.php;
    location / { try_files \$uri \$uri/ /index.php?\$query_string; }
    location ~ \.php\$ {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME /var/www/html/public\$fastcgi_script_name;
        fastcgi_pass php:9000;
    }
}
NGINX
  chmod 600 "$DIR/config/nginx.conf"
}

run_installation() {
  preflight
  validate_dir_and_ports
  ensure_docker
  if [[ ! -f "$DIR/.installer-settings" ]]; then write_new_installation; fi
  if (( HTTP_TO_SSL )); then enable_ssl_on_existing_http; fi
  message 'Starting PostgreSQL, Redis, PHP-FPM and public Nginx...'
  dc up -d
  if (( SSL_ENABLED )) && [[ ! -f "$DIR/certbot/conf/live/$DOMAIN/fullchain.pem" ]]; then
    wait_http "http://127.0.0.1:$HTTP_PORT/" 'Installer v2: PHP-FPM OK' || fail 'Nginx/PHP-FPM did not become ready. Check Docker logs.'
    message "Requesting Let's Encrypt certificate for $DOMAIN (public ports 80/443)..."
    dc run --rm certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
      --webroot -w /var/www/certbot -d "$DOMAIN" || fail 'Certificate issuance failed; HTTP stack remains running. Check DNS and inbound port 80, then rerun.'
  fi
  if (( SSL_ENABLED )); then
    https_config
    dc exec -T nginx nginx -t || fail 'Nginx HTTPS config validation failed.'
    dc exec -T nginx nginx -s reload || fail 'Nginx reload failed.'
    local i body
    for ((i=0;i<20;i++)); do
      body=$(curl -fsS --resolve "$DOMAIN:443:127.0.0.1" --connect-timeout 2 --max-time 4 "https://$DOMAIN/" 2>/dev/null) || body=''
      [[ $body == *'Installer v2: PHP-FPM OK'* ]] && break
      sleep 2
    done
    [[ $body == *'Installer v2: PHP-FPM OK'* ]] || fail 'HTTPS check failed. Inspect Nginx logs and certificate.'
  else
    wait_http "http://127.0.0.1:$HTTP_PORT/" 'Installer v2: PHP-FPM OK' || fail 'HTTP check failed. Inspect Nginx/PHP-FPM logs.'
  fi
  dc exec -T postgres pg_isready -U installer -d installer >/dev/null || fail 'PostgreSQL did not pass the readiness check.'
  [[ $(dc exec -T redis redis-cli ping | tr -d '\r') == PONG ]] || fail 'Redis did not respond to PING.'
  printf '\n  Installation verified.\n'
  printf '  Directory: %s\n' "$DIR"
  if (( SSL_ENABLED )); then
    printf '  URL: https://%s/ (public TCP 443; HTTP 80 for redirects/ACME)\n' "$DOMAIN"
    printf '  Renewal: see README (certificate renewal is not yet automated).\n'
  else
    printf '  URL: http://SERVER_PUBLIC_IP:%s/ (public TCP %s)\n' "$HTTP_PORT" "$HTTP_PORT"
  fi
  printf '  PostgreSQL and Redis have no public ports.\n'
}

main() {
  while :; do
    if ! menu 'MAIN MENU' 'Choose an action:' 'New installation' 'About' 'Exit'; then break; fi
    case "$MENU_CHOICE" in
      0)
        if ! menu 'WEB SERVER' 'Choose how requests reach PHP-FPM:' \
          'Nginx in Docker' 'External web server (coming later)'; then continue; fi
        if (( MENU_CHOICE==1 )); then
          header 'EXTERNAL SERVER'; message 'This option will be implemented after Nginx mode.'; continue
        fi
        nginx_options || continue
        HTTP_PORT=80
        if (( SSL_ENABLED )); then ask_domain || continue; else DOMAIN=''; fi
        ask_dir || continue
        confirm || continue
        run_installation
        break;;
      1) header 'ABOUT'; message 'Bash-only installer for PHP-FPM, PostgreSQL, Redis and Nginx.';;
      2) break;;
    esac
  done
  if (( TTY )); then printf '\033[?25h'; fi
}

main "$@"
