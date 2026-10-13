#!/usr/bin/env bash
# Run from the installed FluidWeb directory (or your chosen directory).
set -euo pipefail
umask 077
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ $EUID == 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
fail() { echo "ERROR: $*" >&2; exit 1; }
dc() { docker compose --project-directory "$DIR" --env-file "$DIR/.env" -f "$DIR/compose.yaml" "$@"; }
require_installation() {
  [[ -f "$DIR/compose.yaml" && -f "$DIR/.env" && -f "$DIR/.installer-settings" ]] || fail 'No complete installation found.'
}
setting() { sed -n "s/^$1=//p" "$2"; }
mode() { if grep -Fxq 'SAVED_MODE=external' "$1"; then echo external; else echo nginx; fi; }
verify() {
  dc exec -T php php public/health.php
  dc exec -T php php bin/console about --env=prod --no-interaction >/dev/null
  [[ $(dc exec -T redis redis-cli ping | tr -d '\r') == PONG ]] || fail 'Redis PING failed.'
  if [[ $(mode "$DIR/.installer-settings") == nginx ]]; then
    dc exec -T nginx nginx -t
    if grep -Fxq 'SAVED_SSL=1' "$DIR/.installer-settings"; then
      local domain
      domain=$(setting SAVED_DOMAIN "$DIR/.installer-settings")
      curl -fsSL --connect-timeout 5 --max-time 20 --resolve "$domain:443:127.0.0.1" "https://$domain/login" >/dev/null
    else
      curl -fsSL --connect-timeout 5 --max-time 20 "http://127.0.0.1:$(setting HTTP_PORT "$DIR/.env")/login" >/dev/null
    fi
  fi
}
checksums() {
  (cd -- "$1"
    find . -type f ! -path ./SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
    while IFS= read -r -d '' link; do
      printf '%q -> %q\n' "$link" "$(readlink -- "$link")"
    done < <(find . -type l -print0 | sort -z)
  )
}
# Backups are self-contained files, not snapshots of live database storage.
backup() (
  require_installation
  local_root="${DIR}-backups"
  [[ ! -L $local_root ]] || fail 'Backup directory must not be a symbolic link.'
  mkdir -p -- "$local_root"
  local_root=$(realpath -e -- "$local_root")
  [[ $local_root != "$DIR" && $local_root != "$DIR/"* ]] || fail 'Backup directory must be outside the installation.'
  chmod 700 "$local_root"
  stage=$(mktemp -d "$local_root/.backup.XXXXXXXX")
  paused=0
  cleanup() {
    result=$?
    trap - EXIT
    if (( paused )); then
      echo 'Restarting the installation after backup...'
      dc up -d --wait --wait-timeout 240 || result=1
    fi
    [[ -z ${archive:-} || ! -f $archive.partial ]] || rm -- "$archive.partial"
    rm -r -- "$stage"
    (( result == 0 )) || echo 'ERROR: Backup failed; check service status and logs.' >&2
    exit "$result"
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  dc config -q
  [[ ! -e "$DIR/data/postgres" ]] || fail 'Legacy PostgreSQL bind storage is unsupported.'
  services=(postgres redis php)
  [[ $(mode "$DIR/.installer-settings") != nginx ]] || services+=(nginx)
  for service in "${services[@]}"; do
    dc ps --status running --services | grep -Fxq "$service" || fail "Start $service before backing up."
  done
  # Refuse links out of the instance; archive internal links as their contents
  # (including Certbot's live -> archive certificate links).
  while IFS= read -r -d '' link; do
    target=$(realpath -e -- "$link") || fail "Broken symbolic link: $link"
    [[ $target == "$DIR/"* ]] || fail "Symbolic link points outside the installation: $link"
    if [[ $link == "$DIR/certbot/"* ]]; then
      certificate_path='^certbot/conf/live/([a-z0-9.-]+)/(cert|chain|fullchain|privkey)\.pem$'
      [[ ${link#"$DIR/"} =~ $certificate_path && ${BASH_REMATCH[1]} != *..* ]] || fail "Unsupported Certbot link: $link"
      domain=${BASH_REMATCH[1]}; part=${BASH_REMATCH[2]}
      target=$(readlink -- "$link")
      [[ $target == "../../archive/$domain/$part"*.pem ]] || fail "Unsupported certificate target: $link"
      number=${target#"../../archive/$domain/$part"}; number=${number%.pem}
      [[ $number =~ ^[0-9]+$ ]] || fail "Unsupported certificate target: $link"
    fi
  done < <(find "$DIR" -type l -print0)
  paused=1
  if [[ $(mode "$DIR/.installer-settings") == nginx ]]; then dc stop nginx; fi
  dc stop php
  dc stop redis
  dc exec -T postgres pg_dump -U installer -d installer --format=custom --no-owner --no-acl > "$stage/database.dump"
  mkdir "$stage/instance"
  for item in .env .installer-settings compose.yaml command.sh config data public; do
    cp -aL -- "$DIR/$item" "$stage/instance/"
  done
  [[ ! -d "$DIR/certbot" ]] || cp -a -- "$DIR/certbot" "$stage/instance/"
  printf '1\n' > "$stage/backup-version"
  printf '%s\n' "$DIR" > "$stage/installation-path"
  chmod --reference="$DIR" "$stage/instance"
  [[ -z $(find "$stage" ! -type f ! -type d ! -type l -print -quit) ]] || fail 'Backup contains an unsupported special file.'
  checksums "$stage" > "$stage/SHA256SUMS"
  archive="$local_root/fluidweb-$(date -u +%Y%m%dT%H%M%SZ)-${stage##*.}.tar.gz"
  tar --hard-dereference -czf "$archive.partial" -C "$stage" .
  gzip -t "$archive.partial"
  mv -- "$archive.partial" "$archive"
  chmod 600 "$archive"
  dc up -d --wait --wait-timeout 240
  paused=0
  verify
  echo "Backup created: $archive"
)
restore() (
  [[ $# == 1 && -f $1 ]] || fail 'Usage: sudo ./command.sh restore /path/to/backup.tar.gz'
  archive=$(realpath -e -- "$1")
  stage=$(mktemp -d /tmp/fluidweb-restore.XXXXXXXX)
  changed=0
  cleanup() {
    result=$?
    trap - EXIT
    [[ ! -f $stage-checksums ]] || rm -- "$stage-checksums"
    rm -r -- "$stage"
    if (( result != 0 && changed )); then
      echo 'ERROR: Restore failed after replacement started. Services may be stopped; inspect logs before retrying the same backup.' >&2
    elif (( result != 0 )); then
      echo 'ERROR: Restore aborted before changing installation data.' >&2
    fi
    exit "$result"
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  gzip -t "$archive"
  tar -tzf "$archive" > "$stage/members"
  : > "$stage/normalized"
  while IFS= read -r member; do
    member=${member#./}
    member=${member%/}
    [[ -z $member ]] && continue
    [[ $member != /* && /$member/ != *'/../'* && /$member/ != *'/./'* && $member != *//* ]] || fail 'Unsafe archive path.'
    printf '%s\n' "$member" >> "$stage/normalized"
    case "$member" in instance|instance/|instance/*|database.dump|backup-version|installation-path|SHA256SUMS) :;; *) fail "Unexpected archive member: $member";; esac
  done < "$stage/members"
  [[ -z $(sort "$stage/normalized" | uniq -d) ]] || fail 'Duplicate archive members.'
  tar -tvzf "$archive" > "$stage/types"
  while IFS= read -r entry; do
    case "${entry:0:1}" in
      -|d) :;;
      l)
        # Certbot renewal needs these relative file links, not dereferenced
        # copies. No directory links or arbitrary extraction targets allowed.
        certificate_link='[[:space:]]\./instance/certbot/conf/live/([a-z0-9.-]+)/(cert|chain|fullchain|privkey)\.pem -> \.\./\.\./archive/([a-z0-9.-]+)/([a-z]+)([0-9]+)\.pem$'
        [[ $entry =~ $certificate_link && ${BASH_REMATCH[1]} == "${BASH_REMATCH[3]}" && ${BASH_REMATCH[2]} == "${BASH_REMATCH[4]}" && ${BASH_REMATCH[1]} != *..* ]] || fail 'Unsupported certificate link in archive.';;
      *) fail 'Archive special files or hard links are unsupported.';;
    esac
  done < "$stage/types"
  rm -- "$stage/members" "$stage/types" "$stage/normalized"
  tar -xzf "$archive" --same-owner --same-permissions -C "$stage"
  [[ $(cat "$stage/backup-version") == 1 ]] || fail 'Unsupported backup version.'
  [[ $(cat "$stage/installation-path") == "$DIR" ]] || fail 'Restore requires the original installation directory (external FastCGI paths are absolute).'
  checksums "$stage" > "$stage-checksums"
  cmp -s "$stage/SHA256SUMS" "$stage-checksums" || { rm -- "$stage-checksums"; fail 'Backup checksum validation failed.'; }
  rm -- "$stage-checksums"
  for item in .env .installer-settings compose.yaml command.sh config/sessions.ini; do
    [[ -f "$stage/instance/$item" ]] || fail "Missing backup file: $item"
  done
  for item in application custom redis var/sessions; do
    [[ -d "$stage/instance/data/$item" ]] || fail "Missing persistent directory: $item"
  done
  [[ ! -e "$stage/instance/data/postgres" ]] || fail 'Backup includes unsupported PostgreSQL physical storage.'
  saved_project=$(setting COMPOSE_PROJECT_NAME "$stage/instance/.env")
  [[ $saved_project =~ ^fluidweb_[a-z0-9]+$ ]] || fail 'Invalid Compose project name.'
  image=$(setting FLUIDWEB_IMAGE "$stage/instance/.env")
  [[ $image =~ ^ghcr\.io/cristake007/fluidweb@sha256:[0-9a-f]{64}$ ]] || fail 'Backup must pin the official FluidWeb image digest.'
  if [[ -f "$DIR/.env" ]]; then
    require_installation
    [[ $(setting COMPOSE_PROJECT_NAME "$DIR/.env") == "$saved_project" ]] || fail 'Backup belongs to a different installation project.'
    [[ $(mode "$DIR/.installer-settings") == $(mode "$stage/instance/.installer-settings") ]] || fail 'Backup uses a different web server mode.'
  else
    [[ -z $(find "$DIR" -mindepth 1 -maxdepth 1 ! -name command.sh -print -quit) ]] || fail 'Unrecognized installation directory; restore into an empty directory containing only command.sh.'
  fi
  # A new host may recover into the same path with only command.sh present.
  while IFS= read -r container; do
    [[ $(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$container") == "$DIR" ]] || fail 'Compose project name belongs to an unrelated container.'
  done < <(docker ps -aq --filter "label=com.docker.compose.project=$saved_project")
  if docker volume inspect "${saved_project}_postgres_data" >/dev/null 2>&1; then
    [[ $(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "${saved_project}_postgres_data") == "$saved_project" ]] || fail 'PostgreSQL volume belongs to an unrelated installation.'
  fi
  if grep -Fxq 'SAVED_SSL=1' "$stage/instance/.installer-settings"; then
    domain=$(setting SAVED_DOMAIN "$stage/instance/.installer-settings")
    [[ $domain =~ ^[a-z0-9.-]+$ && $domain != *..* ]] || fail 'Invalid SSL domain in backup.'
    for file in fullchain.pem privkey.pem; do
      [[ -s "$stage/instance/certbot/conf/live/$domain/$file" ]] || fail "Missing SSL certificate file: $file"
    done
  fi
  docker compose --project-directory "$DIR" --env-file "$stage/instance/.env" -f "$stage/instance/compose.yaml" config -q
  docker run --rm -i --network none --entrypoint pg_restore postgres:18 --no-owner --no-acl --file=/dev/null < "$stage/database.dump"
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  echo "Validated backup: $archive"
  echo "This replaces the database, persistent files, secrets and configuration in $DIR."
  printf 'Type RESTORE to continue: '
  IFS= read -r answer || fail 'Confirmation was not provided.'
  [[ $answer == RESTORE ]] || fail 'Restore cancelled.'
  changed=1
  if [[ -f "$DIR/compose.yaml" && -f "$DIR/.env" ]]; then dc down; fi
  # Only installation-owned paths; never remove Docker volumes or prune resources.
  for item in data config public certbot; do
    [[ ! -e "$DIR/$item" && ! -L "$DIR/$item" ]] || rm -r -- "$DIR/$item"
  done
  cp -a -- "$stage/instance/." "$DIR/"
  chmod 600 "$DIR/.env" "$DIR/.installer-settings" "$DIR/compose.yaml"
  chmod 700 "$DIR/command.sh"
  dc up -d --wait --wait-timeout 120 postgres
  dc exec -T postgres sh -ec 'psql -v ON_ERROR_STOP=1 -U installer -d postgres --set=pw="$POSTGRES_PASSWORD"' <<'SQL'
ALTER ROLE installer WITH PASSWORD :'pw';
SQL
  dc exec -T postgres dropdb -U installer --if-exists --force installer
  dc exec -T postgres createdb -U installer -O installer installer
  dc exec -T postgres pg_restore --exit-on-error --no-owner --no-acl -U installer -d installer < "$stage/database.dump"
  dc up -d --wait --wait-timeout 240
  verify
  echo "Restore completed and verified: $DIR"
)
# Serialize mutations; status and logs remain available during backup/restore.
case "${1:-help}" in
  backup|restore|start|stop|restart|renew-cert)
    exec 9>"${DIR}.command.lock"
    flock -n 9 || fail 'Another installation management command is running.';;
esac
case "${1:-help}" in
  backup) [[ $# == 1 ]] || fail 'Usage: sudo ./command.sh backup'; backup;;
  restore) shift; restore "$@";;
  start) require_installation; dc up -d;;
  stop) require_installation; dc down;;
  restart) require_installation; dc restart;;
  status) require_installation; dc ps;;
  logs) require_installation; shift; dc logs --tail 100 "$@";;
  renew-cert)
    require_installation
    grep -Fxq 'SAVED_SSL=1' "$DIR/.installer-settings" || fail 'SSL was not enabled.'
    dc run --rm certbot renew --non-interactive --webroot -w /var/www/certbot
    dc exec -T nginx nginx -s reload
    ;;
  help) printf 'Usage: sudo ./command.sh {start|stop|restart|status|logs|renew-cert|backup|restore /path/to/backup.tar.gz}\n';;
  *) echo "Unknown command: $1" >&2; exit 2;;
esac
