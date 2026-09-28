#!/usr/bin/env bash
#
# kodi_dev.sh - isolated Kodi dev instances for the parallel 1.x and 2.x lines.
#
# WHY THIS EXISTS
#   Kodi's add-on installer DELETES the existing add-on directory before moving
#   the new files into place, and that delete follows symlinks. A git checkout
#   symlinked into an addons directory is therefore destroyed the moment Kodi
#   installs the same add-on id from a repository - which is exactly what
#   happened on 2026-09-15 when 1.0.90 hit the official repo (the install then
#   also failed, because the now-dangling symlink still occupied the path).
#
#   The rule this script enforces: a checkout is only ever symlinked into a Kodi
#   instance that cannot install this add-on from a repository. Everywhere else
#   we deploy a COPY, which Kodi may freely delete.
#
# LINES
#   1x -> maintenance of the released line   (branch master,  worktree ../<addon>-1x)
#   2x -> next major line                    (branch develop, this checkout)
#   Both use the SAME add-on id, so they can never live in one Kodi profile.
#   Each gets its own Kodi instance, isolated by HOME.
#
# USAGE
#   scripts/kodi_dev.sh setup 1x|2x     create the worktree + dev instance skeleton
#   scripts/kodi_dev.sh deploy 1x|2x    copy the line's checkout into its instance
#   scripts/kodi_dev.sh run 1x|2x       launch Kodi against that instance
#   scripts/kodi_dev.sh log 1x|2x       stream that instance's add-on log lines
#   scripts/kodi_dev.sh guard           fail if any unsafe symlink exists
#   scripts/kodi_dev.sh status          show every instance and how it is wired
#
set -euo pipefail

ADDON_ID="service.subtitles.opensubtitles-com"
REPO_ID="repository.opensubtitles-com"
DEV_ROOT="${KODI_DEV_ROOT:-$HOME/.kodi-dev}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

# ---------------------------------------------------------------- path helpers

# addons dir inside a given Kodi HOME
addons_dir_for_home() {
  case "$(uname -s)" in
    Darwin) printf '%s/Library/Application Support/Kodi/addons' "$1" ;;
    *)      printf '%s/.kodi/addons' "$1" ;;
  esac
}

log_file_for_home() {
  case "$(uname -s)" in
    Darwin) printf '%s/Library/Logs/kodi.log' "$1" ;;
    *)      printf '%s/.kodi/temp/kodi.log' "$1" ;;
  esac
}

kodi_binary() {
  case "$(uname -s)" in
    Darwin) printf '/Applications/Kodi.app/Contents/MacOS/Kodi' ;;
    *)      command -v kodi || printf 'kodi' ;;
  esac
}

# the checkout that holds a given line
src_for_line() {
  case "$1" in
    2x) printf '%s' "$REPO_DIR" ;;
    1x) printf '%s-1x' "$REPO_DIR" ;;
    *)  die "unknown line '$1' (expected 1x or 2x)" ;;
  esac
}

branch_for_line() {
  case "$1" in
    2x) printf 'develop' ;;
    1x) printf 'master' ;;
  esac
}

home_for_line() {
  case "$1" in
    1x|2x) printf '%s/%s' "$DEV_ROOT" "$1" ;;
    *)     die "unknown line '$1' (expected 1x or 2x)" ;;
  esac
}

require_line() { [ $# -ge 1 ] && [ -n "${1:-}" ] || die "a line is required: 1x or 2x"; }

# ---------------------------------------------------------------------- guard

# A symlinked checkout is only safe in a dev instance that has no repository
# able to serve this add-on id. Anything else is the configuration that ate the
# working copy on 2026-09-15.
check_one_addons_dir() {
  local addons="$1" label="$2" strict="$3" rc=0
  local target="$addons/$ADDON_ID"

  [ -d "$addons" ] || { note "  $label: no addons dir (not set up)"; return 0; }

  if [ -L "$target" ]; then
    if [ "$strict" = "strict" ]; then
      note "  $label: UNSAFE - $ADDON_ID is a symlink -> $(readlink "$target")"
      note "           a repo install here will delete the checkout it points at"
      rc=1
    elif [ -d "$addons/$REPO_ID" ]; then
      note "  $label: UNSAFE - symlinked checkout AND $REPO_ID installed"
      note "           remove the repository from this instance, or deploy a copy instead"
      rc=1
    elif [ ! -e "$target" ]; then
      note "  $label: dangling symlink -> $(readlink "$target") (blocks every install)"
      rc=1
    else
      note "  $label: symlink -> $(readlink "$target") (allowed: no repository here)"
    fi
  elif [ -d "$target" ]; then
    note "  $label: plain copy (safe)"
  else
    note "  $label: $ADDON_ID not present"
  fi
  return $rc
}

cmd_guard() {
  local rc=0
  note "Checking Kodi instances for unsafe $ADDON_ID wiring:"
  # The real profile must NEVER hold a symlink - it is the one that installs
  # from the official repository.
  check_one_addons_dir "$(addons_dir_for_home "$HOME")" "default profile ($HOME)" strict || rc=1
  for line in 1x 2x; do
    local h; h="$(home_for_line "$line")"
    check_one_addons_dir "$(addons_dir_for_home "$h")" "dev $line ($h)" lenient || rc=1
  done
  [ $rc -eq 0 ] && note "OK - no unsafe wiring found." || note "FAILED - fix the entries above before installing anything."
  return $rc
}

# ---------------------------------------------------------------------- setup

cmd_setup() {
  local line="$1" src home addons
  src="$(src_for_line "$line")"
  home="$(home_for_line "$line")"
  addons="$(addons_dir_for_home "$home")"

  if [ "$line" = "1x" ] && [ ! -d "$src" ]; then
    note "Creating 1.x worktree at $src (branch $(branch_for_line 1x))"
    git -C "$REPO_DIR" worktree add "$src" "$(branch_for_line 1x)"
  fi
  [ -d "$src" ] || die "checkout for $line missing at $src"

  mkdir -p "$addons"
  note "Dev instance for $line ready:"
  note "  checkout : $src"
  note "  KODI HOME: $home"
  note "  addons   : $addons"
  note ""
  note "Next: scripts/kodi_dev.sh deploy $line && scripts/kodi_dev.sh run $line"
  note "Inside that Kodi, do NOT install $REPO_ID and leave add-on auto-update off."
}

# --------------------------------------------------------------------- deploy

cmd_deploy() {
  local line="$1" src home addons dest
  src="$(src_for_line "$line")"
  home="$(home_for_line "$line")"
  addons="$(addons_dir_for_home "$home")"
  dest="$addons/$ADDON_ID"

  [ -d "$src" ] || die "checkout for $line missing at $src - run: scripts/kodi_dev.sh setup $line"
  [ -d "$addons" ] || die "dev instance for $line missing - run: scripts/kodi_dev.sh setup $line"
  [ -L "$dest" ] && die "$dest is a symlink - remove it first, this script deploys copies only"

  # A copy, never a link: Kodi may delete this freely without touching git.
  rsync -a --delete \
    --exclude '.git' --exclude '.github' --exclude '.githooks' \
    --exclude 'tests' --exclude 'scripts' --exclude 'docs' --exclude 'dist' \
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
    --exclude '.env' \
    "$src/" "$dest/"

  note "Deployed $line: $src -> $dest"
  note "Restart Kodi (or disable/enable the add-on) for background-service changes to load."
}

# ------------------------------------------------------------------- run / log

cmd_run() {
  local line="$1" home bin
  home="$(home_for_line "$line")"
  bin="$(kodi_binary)"
  [ -d "$home" ] || die "dev instance for $line missing - run: scripts/kodi_dev.sh setup $line"
  [ -x "$bin" ] || die "Kodi binary not found at $bin"

  # Kodi derives its whole profile from HOME (verified in xbmc
  # SettingsComponent.cpp, InitDirectoriesOSX), so this is a fully separate
  # instance: own addons, own userdata, own log, own library.
  note "Launching Kodi with HOME=$home"
  HOME="$home" "$bin" &
}

cmd_log() {
  local line="$1" home file
  home="$(home_for_line "$line")"
  file="$(log_file_for_home "$home")"
  [ -f "$file" ] || die "no log yet at $file - start the instance first"
  tail -f "$file" | grep --line-buffered -E "$ADDON_ID|OpenSubtitles"
}

# -------------------------------------------------------------------- status

cmd_status() {
  note "add-on id : $ADDON_ID"
  note "repo dir  : $REPO_DIR"
  note "dev root  : $DEV_ROOT"
  note ""
  for line in 1x 2x; do
    local src home
    src="$(src_for_line "$line")"; home="$(home_for_line "$line")"
    note "line $line:"
    note "  checkout : $src $([ -d "$src" ] && printf '(present, branch %s)' "$(git -C "$src" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')" || printf '(missing)')"
    note "  instance : $home $([ -d "$home" ] && echo '(present)' || echo '(missing)')"
  done
  note ""
  cmd_guard || true
}

# ----------------------------------------------------------------------- main

case "${1:-}" in
  setup)  require_line "${2:-}"; cmd_setup "$2" ;;
  deploy) require_line "${2:-}"; cmd_deploy "$2" ;;
  run)    require_line "${2:-}"; cmd_run "$2" ;;
  log)    require_line "${2:-}"; cmd_log "$2" ;;
  guard)  cmd_guard ;;
  status) cmd_status ;;
  *)      sed -n '3,30p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
