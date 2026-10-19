# ABOUTME: Build, test and release recipes for slarc.
# ABOUTME: `just check` is what CI and the pre-commit hook run.

default: check

# Run all checks (test + lint + typecheck + format check)
check: test lint typecheck fmt-check

# Run tests; pass pytest args, e.g. `just test -k links`
test *ARGS:
    uv run pytest {{ARGS}}

# Run ruff lints
lint:
    uv run ruff check .

# Run the ty type checker
typecheck:
    uv run ty check src tests

# Format code
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Check formatting without modifying
fmt-check:
    uv run ruff format --check .

# Build sdist and wheel into dist/
build:
    uv build

# Install slarc on PATH as an editable uv tool that tracks this working tree
install:
    uv tool install --editable --force .

# Run slarc from the working tree with arbitrary arguments
run *ARGS:
    uv run slarc {{ARGS}}

# Remove build artifacts and caches
clean:
    rm -rf dist/ .pytest_cache/ .ruff_cache/ .hypothesis/
    find . -name __pycache__ -type d -not -path './.venv/*' -not -path './.llm/*' -prune -exec rm -rf {} +

# Install git pre-commit hook that runs all checks before each commit
install-hooks:
    #!/usr/bin/env bash
    set -euo pipefail
    hook=".git/hooks/pre-commit"
    cat > "$hook" << 'HOOK'
    #!/bin/sh
    just check
    HOOK
    chmod +x "$hook"
    echo "Installed pre-commit hook: $hook"

# Bump version, commit, tag with release notes, and push.
# Usage: just bump 1.2.3 (or just bump for patch increment)
bump version="":
    #!/usr/bin/env bash
    set -euo pipefail
    current=$(uv version --short)
    if [ -z "{{version}}" ]; then
        IFS='.' read -r major minor patch <<< "$current"
        new="$major.$minor.$((patch + 1))"
    else
        new="{{version}}"
    fi
    echo "Bumping $current -> $new"
    if [ "$current" != "$new" ]; then
        uv version "$new"
        git add pyproject.toml uv.lock
        git commit -m "Bump version to $new"
    fi
    prev_tag=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
    if [ -n "$prev_tag" ]; then
        log=$(git log "$prev_tag"..HEAD --oneline --no-merges)
    else
        log=$(git log --oneline --no-merges)
    fi
    notes_file=$(mktemp)
    trap 'rm -f "$notes_file"' EXIT
    if command -v claude >/dev/null 2>&1; then
        claude -p "Generate concise release notes for version $new. Commits:\n$log\n\nGuidelines: group related commits, focus on user-facing changes, skip version bumps and CI changes, one line per bullet, past tense, output only a bullet list." > "$notes_file" 2>/dev/null || echo "$log" | sed 's/^[0-9a-f]* /- /' > "$notes_file"
    else
        echo "$log" | sed 's/^[0-9a-f]* /- /' > "$notes_file"
    fi
    echo "Release notes:"
    cat "$notes_file"
    git tag -a "v$new" -F "$notes_file"
    git push && git push --tags
    echo "v$new released!"

# Delete a GitHub release and re-tag to re-trigger the release workflow.
# Preserves the annotated tag message (release notes).
# Usage: just retag 1.2.3
retag version:
    #!/usr/bin/env bash
    set -euo pipefail
    tag="v{{version}}"
    notes=$(git tag -l --format='%(contents)' "$tag" 2>/dev/null || echo "$tag")
    notes_file=$(mktemp)
    trap 'rm -f "$notes_file"' EXIT
    echo "$notes" > "$notes_file"
    gh release delete "$tag" --yes || true
    git push origin ":refs/tags/$tag" || true
    git tag -d "$tag" || true
    git tag -a "$tag" -F "$notes_file"
    git push && git push --tags
