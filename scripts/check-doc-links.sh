#!/usr/bin/env bash
# Resolve relative markdown links in tracked *.md files against the filesystem.
# Reports only; exits 1 if any relative link target is missing. External (scheme://,
# mailto:) and pure-anchor (#...) links are ignored — only on-disk targets are checked.
# NOT scanned: docs/archive/** (frozen history) and docs/design/** (narrative specs) —
# same exemption as check-doc-paths.sh, so a restructure that re-nests archived docs does
# not turn their now-stale links into gate failures we'd have to "fix" by editing history.
# Usage: check-doc-links.sh [ROOT]   (ROOT defaults to the repo root / cwd)
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

# Collect markdown files: tracked files when in a git tree, else every *.md under ROOT
# (the test harness passes a non-git tmp dir). NOT scanned: docs/archive/** (frozen
# history — its links pointed at the tree as it was and must not be rewritten) and
# docs/design/** (design specs narrate moves and may show illustrative links).
mapfile -t files < <(
  { git ls-files '*.md' 2>/dev/null || true; } | grep -vE '^docs/(design|archive)/'
)
if ((${#files[@]} == 0)); then
  mapfile -t files < <(
    find . -type f -name '*.md' \
      -not -path './docs/design/*' -not -path './docs/archive/*' -printf '%P\n'
  )
fi

broken=0
for f in "${files[@]}"; do
  dir="$(dirname "$f")"
  # Extract [text](target) targets; drop the #fragment; one per line. Fenced code blocks
  # are stripped first (the awk toggles on triple-backtick fence lines; \140 is the octal
  # for a backtick, used so this script contains no literal fence marker) so illustrative
  # example links inside code samples are not treated as real cross-references.
  while IFS= read -r target; do
    [[ -z "$target" ]] && continue
    case "$target" in
    *"://"* | mailto:* | "#"*) continue ;;
    esac
    # strip a trailing CommonMark title:  [t](dest "title")  -> dest
    target="${target%% *}"
    target="${target%%#*}"
    [[ -z "$target" ]] && continue
    if [[ ! -e "${dir}/${target}" ]]; then
      printf "broken link: %s -> %s\n" "$f" "$target" >&2
      broken=1
    fi
  done < <(awk 'BEGIN { fence = 0 } /^\140\140\140/ { fence = !fence; next } !fence' "$f" |
    grep -oE '\]\([^)]+\)' | sed -E 's/^\]\(//; s/\)$//')
done

if ((broken)); then
  printf "\nmarkdown link check failed\n" >&2
  exit 1
fi
printf "markdown links resolve\n"
