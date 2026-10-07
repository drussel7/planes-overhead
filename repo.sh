#!/bin/bash
# Planes Overhead - keeps this folder in a private GitHub repo.
#
#   bash repo.sh                 show what has changed since the last save
#   bash repo.sh --push "note"   save the changes and send them to GitHub
set -e
cd "$(dirname "$0")"
NAME=planes-overhead
OTHER="$HOME/navigans-tools"     # an existing repo to copy the GitHub details from

hide() { sed -E 's#//[^@/]*@#//***@#'; }   # never print a saved password

if ! command -v git >/dev/null 2>&1; then
    echo "Git is not installed. Run: sudo apt-get install -y git"
    exit 1
fi
if [ ! -f .gitignore ] || ! grep -qx "config.yaml" .gitignore; then
    echo "STOPPED: .gitignore is missing or does not exclude config.yaml."
    exit 1
fi

if [ ! -d .git ]; then
    git init -q -b main
    echo "Created a new repo in $(pwd)"
fi
for key in user.name user.email; do
    if ! git config "$key" >/dev/null; then
        value="$(git -C "$OTHER" config "$key" 2>/dev/null || true)"
        [ -z "$value" ] && { [ "$key" = user.name ] && value="$(id -un)" || value="$(id -un)@$(hostname)"; }
        git config "$key" "$value"
    fi
done
if ! git remote get-url origin >/dev/null 2>&1; then
    source_url="$(git -C "$OTHER" remote get-url origin 2>/dev/null || true)"
    if [ -n "$source_url" ]; then
        git remote add origin "$(echo "$source_url" | sed -E "s#[^/:]+\$#$NAME.git#")"
        echo "GitHub address set from navigans-tools: $(git remote get-url origin | hide)"
    fi
fi

git add -A

# never let the private values out, whatever file they turn up in
if git diff --cached --name-only | grep -qx "config.yaml"; then
    echo "STOPPED: config.yaml was about to be saved to the repo."
    git reset -q
    exit 1
fi
if [ -f config.yaml ]; then
    while IFS= read -r secret; do
        [ -z "$secret" ] && continue
        if git grep --cached -q -F -e "$secret"; then
            echo "STOPPED: a private value from config.yaml appears in:"
            git grep --cached -l -F -e "$secret" | sed 's/^/  /'
            git reset -q
            exit 1
        fi
    done < <(python3 - <<'PY'
import yaml
cfg = yaml.safe_load(open("config.yaml")) or {}
for value in ((cfg.get("trmnl") or {}).get("webhook_url"), (cfg.get("alerts") or {}).get("ntfy_topic")):
    if value and len(str(value)) >= 8:
        print(value)
PY
)
fi

if [ "$1" != "--push" ]; then
    if git diff --cached --quiet; then
        echo "Nothing has changed since the last save."
    else
        echo "Changed since the last save:"
        git diff --cached --name-status | sed 's/^/  /'
        echo "Save with: bash repo.sh --push \"what changed\""
    fi
    git reset -q
    exit 0
fi

if git diff --cached --quiet; then
    echo "Nothing new to save."
else
    git commit -q -m "${2:-update}"
    echo "Saved: $(git log -1 --format='%h %s')"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
    echo "Saved on the Pi only - no GitHub address is set."
    echo "Create a private repo called $NAME on GitHub, then run:"
    echo "  git -C $(pwd) remote add origin <its address>"
    exit 1
fi
if git push -q -u origin main; then
    echo "Sent to GitHub: $(git remote get-url origin | hide)"
else
    echo
    echo "Saved on the Pi, but the push to GitHub failed (see the message above)."
    echo "If the repo does not exist yet: go to https://github.com/new, name it $NAME,"
    echo "choose Private, leave the README and .gitignore options off, then run this again."
    exit 1
fi
