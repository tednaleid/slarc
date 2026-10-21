#!/usr/bin/env bash
# ABOUTME: One-time setup: creates the tednaleid/homebrew-slarc tap repo on GitHub and seeds it
# ABOUTME: with the formula for slarc's latest release, then says how to set HOMEBREW_TAP_TOKEN.
set -euo pipefail

OWNER="tednaleid"
TAP_REPO="homebrew-slarc"
MAIN_REPO="slarc"
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"

if ! command -v gh &>/dev/null; then
    echo "Error: gh CLI is required. Install with: brew install gh"
    exit 1
fi
if ! gh auth status &>/dev/null; then
    echo "Error: not authenticated with gh. Run: gh auth login"
    exit 1
fi

echo "Fetching the latest release..."
TAG=$(gh release view --repo "${OWNER}/${MAIN_REPO}" --json tagName -q .tagName)
VERSION="${TAG#v}"
SDIST="https://github.com/${OWNER}/${MAIN_REPO}/releases/download/${TAG}/slarc-${VERSION}.tar.gz"
SHA256=$(curl -fsSL "$SDIST" | shasum -a 256 | awk '{print $1}')
echo "  ${TAG}: slarc-${VERSION}.tar.gz ${SHA256}"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

if gh repo view "${OWNER}/${TAP_REPO}" &>/dev/null; then
    echo "Repo ${OWNER}/${TAP_REPO} already exists, cloning."
    git clone "git@github.com:${OWNER}/${TAP_REPO}.git" tap
else
    echo "Creating ${OWNER}/${TAP_REPO}..."
    gh repo create "${OWNER}/${TAP_REPO}" --public --description "Homebrew tap for slarc"
    git clone "git@github.com:${OWNER}/${TAP_REPO}.git" tap
fi
cd tap

mkdir -p Formula
"$SCRIPTS/homebrew-formula.sh" "$VERSION" "$SHA256" > Formula/slarc.rb

cat > README.md << 'README'
# homebrew-slarc

Homebrew tap for [slarc](https://github.com/tednaleid/slarc).

## Install

```bash
brew install tednaleid/slarc/slarc
```

Or:

```bash
brew tap tednaleid/slarc
brew install slarc
```

## Update

```bash
brew upgrade slarc
```
README

git add Formula/ README.md
if git diff --cached --quiet; then
    echo "The tap already holds this formula."
else
    git commit -m "Formula for slarc ${VERSION}"
    git push -u origin HEAD
fi

echo ""
echo "Tap repo ready at: https://github.com/${OWNER}/${TAP_REPO}"
echo ""
echo "-- Next step: create a fine-grained Personal Access Token --"
echo ""
echo "1. Go to: https://github.com/settings/personal-access-tokens/new"
echo "2. Token name: ${MAIN_REPO}-homebrew-tap"
echo "3. Repository access: Only select repositories -> ${OWNER}/${TAP_REPO}"
echo "4. Permissions: Contents -> Read and write"
echo "5. Generate the token and copy it"
echo ""
echo "Then set it as a secret on the ${MAIN_REPO} repo:"
echo ""
echo "  gh secret set HOMEBREW_TAP_TOKEN --repo ${OWNER}/${MAIN_REPO}"
echo ""
echo "(Paste the token when prompted.)"
