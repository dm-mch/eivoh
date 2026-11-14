#!/usr/bin/env bash
# Explicitly deploy a committed revision; never modifies the executor work tree.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || (${2:-} != '' && ${2:-} != --restart) ]]; then
    echo 'Usage: deploy/release.sh <commit> [--restart]' >&2
    exit 2
fi
source_dir=$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)
revision=$(git -C "$source_dir" rev-parse --verify --end-of-options "$1^{commit}")
release_dir="$HOME/.local/share/eivoh/run"
mkdir -p "$(dirname "$release_dir")"
if [[ -e "$release_dir" ]]; then
    # Refuse a normal clone or an unrelated directory; no destructive reset/clean.
    [[ -f "$release_dir/.git" ]] || { echo 'Release path is not a worktree' >&2; exit 1; }
    [[ -z $(git -C "$release_dir" status --porcelain) ]] || { echo 'Release worktree has changes' >&2; exit 1; }
    previous=$(git -C "$release_dir" rev-parse HEAD)
    echo "Previous commit (rollback): $previous"
    git -C "$release_dir" checkout --detach "$revision"
else
    echo 'Previous commit (rollback): none (first release)'
    git -C "$source_dir" worktree add --detach "$release_dir" "$revision"
fi
# Keep generated dependency inputs in the ignored venv, not the source tree.
cd "$release_dir"
uv venv --python 3.12 --allow-existing .venv
uv pip compile pyproject.toml --extra dev --output-file .venv/release-requirements.txt
uv pip sync --python .venv/bin/python .venv/release-requirements.txt
uv pip install --python .venv/bin/python --no-deps .
EIVOH_LIVE=0 .venv/bin/python -m pytest -q -m 'not live'
# After validation, so a rejected release never touches the home. A revision
# before V3.1 (rollback) has no templates and no seeding module.
if [[ -d "$release_dir/templates/home" ]]; then
    # Seed missing home files and report template changes; never overwrites the home.
    .venv/bin/python -m eivoh.home seed --home "${EIVOH_HOME:-$HOME/eivoh-home}" --templates "$release_dir/templates/home" \
        --core "$source_dir" --commit "$revision"
fi
echo "Validated release: $revision"
if [[ ${2:-} == --restart ]]; then
    systemctl --user restart eivoh.service
else
    echo 'Service not restarted. Use --restart to request it explicitly.'
fi
