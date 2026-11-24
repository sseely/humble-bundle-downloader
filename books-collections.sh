#!/usr/bin/env bash
# Put every Apple Books book into a collection named after its Humble bundle.
#
#   ./books-collections.sh          scan Books, plan, then add books to collections
#   ./books-collections.sh --plan   scan and plan only; changes nothing in Books
#   ./books-collections.sh --info   scan, then look up titles and authors into
#                                   book_info.json for review; changes nothing
#   ./books-collections.sh --rename apply book_info.json: retitle entries and
#                                   set authors in Books
#   ./books-collections.sh --remove delete the duplicate copies in remove.json
#                                   from Books and iCloud (Delete Everywhere)
#
# Drives the Books UI through macOS accessibility. Books must stay frontmost
# and the screen unlocked while it runs; it stops (progress saved) otherwise.
# Re-run at any time: books already added are skipped.
set -euo pipefail

cd "$(dirname "$0")"
SCRIPTS=scripts/books_collections

if [[ "$(uname)" != Darwin ]]; then
  echo "books-collections.sh needs macOS (Apple Books)." >&2
  exit 1
fi

# Comic Vine API key (optional): env var, else the macOS Keychain entry
# created with: security add-generic-password -a "$USER" -s humble-dl-comicvine -w
KEYCHAIN_SERVICE=humble-dl-comicvine

mode=collections
case "${1:-}" in
  "") ;;
  --plan) mode=plan ;;
  --info) mode=info ;;
  --rename) mode=rename ;;
  --remove) mode=remove ;;
  *) echo "usage: $0 [--plan | --info | --rename | --remove]" >&2; exit 1 ;;
esac

run() { caffeinate -dimsu uv run --group macos python "$@"; }

# Opening Books brings back its library window if only reader windows are open.
open -a Books
osascript -e 'tell application "Books" to activate'
sleep 2

if [[ $mode == rename ]]; then
  run "$SCRIPTS/rename_pass.py"
  exit 0
fi

if [[ $mode == remove ]]; then
  run "$SCRIPTS/remove_pass.py"
  exit 0
fi

run "$SCRIPTS/scan.py"

if [[ $mode == info ]]; then
  key="${HUMBLE_DL_COMICVINE_API_KEY:-$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)}"
  HUMBLE_DL_COMICVINE_API_KEY="$key" uv run python "$SCRIPTS/info.py"
  exit 0
fi

run "$SCRIPTS/plan.py"

if [[ $mode == plan ]]; then
  exit 0
fi

osascript -e 'tell application "Books" to activate'
sleep 2
run "$SCRIPTS/run_pass.py"
