#!/usr/bin/env bash
# PostToolUse hook: commit and push any file Claude edits inside this repo.
# Reads the hook's JSON payload on stdin.
set -euo pipefail

file=$(jq -r '.tool_response.filePath // .tool_input.file_path // .tool_input.notebook_path // empty')
[[ -n "$file" ]] || exit 0

repo=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ "$repo" -ef "${CLAUDE_PROJECT_DIR:-$repo}" ]] || exit 0

cd "$repo"
rel=$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1]))' "$file")

# Ignored files (e.g. .env) are skipped: git add refuses them.
git add -- "$rel" 2>/dev/null || exit 0
git diff --cached --quiet && exit 0

git commit -q -m "Update $rel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -q
