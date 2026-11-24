#!/usr/bin/env bash
# Download new ebooks, import them into Apple Books, fix titles and authors,
# delete duplicate copies, then file every book into its bundle's collection.
#
#   ./sync-books.sh          asks before retitling and before deleting copies
#   ./sync-books.sh --yes    asks nothing (unattended)
#
# Steps, in this order so collections are planned from final titles and never
# get a copy that is about to be deleted (each step can also be run alone):
#   1. humble-dl sync                 download new files          (browser window)
#   2. humble-dl titles               Humble's exact item titles  (browser window)
#   3. humble-dl books                import new files into Books
#   4. books-collections.sh --info    scan Books; plan titles, authors, duplicates
#   5. books-collections.sh --rename  retitle and set authors     (asks first)
#   6. books-collections.sh --remove  delete duplicate copies     (asks first)
#   7. books-collections.sh           file books into collections
# Steps 4-7 drive the Books app: keep it in front and don't use the Mac.
set -euo pipefail

cd "$(dirname "$0")"

STATE_DIR="${HUMBLE_DL_COLLECTIONS_DIR:-$HOME/.humble-dl/collections}"
EXIT_SOME_FILES_FAILED=4  # humble-dl: the run finished but some files or bundles failed
EXIT_ENTRIES_LEFT=3       # a Books pass finished but left entries for a later re-run

assume_yes=false
case "${1:-}" in
  "") ;;
  --yes) assume_yes=true ;;
  *) echo "usage: $0 [--yes]" >&2; exit 1 ;;
esac

warnings=()

step() { printf '\n== %s\n' "$*"; }

# run_step <tolerated exit code> <command...>: that exit code is a warning
# (partial result; a later run picks up the rest); any other failure stops.
run_step() {
  local tolerated=$1
  shift
  local status=0
  "$@" || status=$?
  if ((status == 0)); then
    return 0
  fi
  if ((status == tolerated)); then
    warnings+=("$* finished with exit $status (partial; re-run later)")
    return 0
  fi
  echo "stopped: $* failed with exit $status" >&2
  exit "$status"
}

# confirm <question>: true on yes; never asks with --yes; refuses without a terminal.
confirm() {
  if $assume_yes; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "stopped: no terminal to ask '$1'; re-run with --yes to skip questions" >&2
    exit 1
  fi
  local reply
  read -r -p "$1 [y/N] " reply
  [[ $reply == [yY]* ]]
}

# entries <json file>: how many entries it lists (0 if missing).
entries() {
  if [[ -f $1 ]]; then
    python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))))' "$1"
  else
    echo 0
  fi
}

step "1/7 download new ebooks"
run_step "$EXIT_SOME_FILES_FAILED" uv run humble-dl sync

step "2/7 save Humble's item titles"
run_step "$EXIT_SOME_FILES_FAILED" uv run humble-dl titles

step "3/7 import new files into Books"
run_step "$EXIT_SOME_FILES_FAILED" uv run humble-dl books

step "4/7 plan titles, authors and duplicates"
run_step 0 ./books-collections.sh --info

renames=$(entries "$STATE_DIR/book_info.json")
step "5/7 retitle and set authors: $renames entries"
if ((renames == 0)); then
  echo "nothing to change"
elif confirm "Apply $renames title/author changes from $STATE_DIR/book_info.json?"; then
  run_step "$EXIT_ENTRIES_LEFT" ./books-collections.sh --rename
else
  warnings+=("titles and authors not applied (answered no)")
fi

removals=$(entries "$STATE_DIR/remove.json")
step "6/7 delete duplicate copies: $removals entries"
if ((removals == 0)); then
  echo "no duplicates"
elif confirm "Delete $removals duplicate copies in $STATE_DIR/remove.json from Books and iCloud?"; then
  run_step "$EXIT_ENTRIES_LEFT" ./books-collections.sh --remove
else
  warnings+=("duplicate copies not deleted (answered no)")
fi

step "7/7 file books into their bundle collections"
run_step "$EXIT_ENTRIES_LEFT" ./books-collections.sh

step "done"
if ((${#warnings[@]})); then
  printf 'warning: %s\n' "${warnings[@]}"
else
  echo "all steps completed"
fi
