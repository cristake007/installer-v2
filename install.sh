#!/usr/bin/env bash
# Installer v2 — Bash-only terminal interface. Preview mode: no host changes.
set -euo pipefail

INTERACTIVE=0
if [[ -t 0 && -t 1 && "${TERM:-dumb}" != dumb ]]; then
  INTERACTIVE=1
fi

if (( INTERACTIVE )); then
  BLUE=$'\033[1;34m'
  CYAN=$'\033[1;36m'
  DIM=$'\033[2m'
  BOLD=$'\033[1m'
  RESET=$'\033[0m'
else
  BLUE='' CYAN='' DIM='' BOLD='' RESET=''
fi

restore_terminal() {
  if (( INTERACTIVE )); then printf '\033[?25h\033[0m'; fi
}
trap restore_terminal EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

clear_screen() {
  if (( INTERACTIVE )); then
    printf '\033[2J\033[H\033[?25l'
  else
    printf '\n'
  fi
}

header() {
  local section="$1" caption="$2"
  clear_screen
  printf '  %s╭──────────────────────────────────────────────────────────────╮%s\n' "$BLUE" "$RESET"
  printf '  %s│%s  %s%-58s%s%s│%s\n' "$BLUE" "$RESET" "$BOLD" 'INSTALLER V2' "$RESET" "$BLUE" "$RESET"
  printf '  %s│%s  %-58s%s│%s\n' "$BLUE" "$RESET" 'Docker infrastructure · Terminal setup' "$BLUE" "$RESET"
  printf '  %s╰──────────────────────────────────────────────────────────────╯%s\n\n' "$BLUE" "$RESET"
  printf '  %s%s%s\n  %s%s%s\n\n' "$BOLD" "$section" "$RESET" "$DIM" "$caption" "$RESET"
}

# Sets MENU_CHOICE (zero-based); Esc or EOF returns 1.
menu() {
  local title="$1" description="$2"
  shift 2
  local -a options=("$@")
  local selected=0 key sequence index number
  if ((${#options[@]} == 0)); then return 1; fi

  while :; do
    header "$title" "$description"
    for ((index=0; index<${#options[@]}; index++)); do
      if (( INTERACTIVE && index == selected )); then
        printf '  %s  ❯  %s%s\n' "$CYAN" "${options[index]}" "$RESET"
      else
        printf '       %s\n' "${options[index]}"
      fi
    done
    printf '\n  %s↑/↓ Navigate   Enter Select   Esc Back   1–%d Quick select%s\n' \
      "$DIM" "${#options[@]}" "$RESET"

    if (( INTERACTIVE )); then
      key=''
      if ! IFS= read -rsn1 key; then return 1; fi
      case "$key" in
        $'\033')
          sequence=''
          IFS= read -rsn2 -t 0.1 sequence || true
          case "$sequence" in
            '[A') selected=$(( (selected + ${#options[@]} - 1) % ${#options[@]} )) ;;
            '[B') selected=$(( (selected + 1) % ${#options[@]} )) ;;
            '') return 1 ;;
          esac
          ;;
        '') MENU_CHOICE="$selected"; return 0 ;;
        [1-9])
          number=$((key - 1))
          if (( number < ${#options[@]} )); then
            MENU_CHOICE="$number"
            return 0
          fi
          ;;
      esac
    else
      printf '  Selection [1-%d, q=exit]: ' "${#options[@]}"
      if ! IFS= read -r key; then return 1; fi
      if [[ "$key" == 'q' || "$key" == 'Q' ]]; then return 1; fi
      if [[ "$key" =~ ^[1-9]$ ]] && (( 10#$key <= ${#options[@]} )); then
        MENU_CHOICE=$((10#$key - 1))
        return 0
      fi
      printf '  Invalid selection.\n'
    fi
  done
}

show_message() {
  header "$1" "$2"
  printf '  %s\n\n' "$3"
  if (( INTERACTIVE )); then
    printf '  %sPress any key to return...%s\n' "$DIM" "$RESET"
    local unused
    IFS= read -rsn1 unused || true
  fi
}

main() {
  local web_mode
  while :; do
    if ! menu 'MAIN MENU' 'Select an option to continue.' \
      'New installation' 'About' 'Exit'; then break; fi
    case "$MENU_CHOICE" in
      0)
        if ! menu 'WEB SERVER' 'Choose how HTTP traffic will be handled.' \
          'Nginx in Docker (default)' 'External server on host'; then continue; fi
        if (( MENU_CHOICE == 0 )); then
          web_mode='Nginx in Docker'
        else
          web_mode='External web server'
        fi
        show_message 'INSTALLATION PREVIEW' 'Interface demonstration — no changes will be made.' \
          "Web server: $web_mode
  Install directory: /opt/installer-v2

  Installation logic will be added in the next stage."
        ;;
      1)
        show_message 'ABOUT' 'Simple. Predictable. No unnecessary dependencies.' \
          'Installer v2 — Bash-only terminal interface.
  No Docker commands or host changes are performed yet.'
        ;;
      2) break ;;
    esac
  done
  clear_screen
  printf '  Installer closed. No changes were made.\n'
}

main "$@"
