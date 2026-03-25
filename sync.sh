#!/bin/bash
# DollOS Sync — clone or pull all repos into ~/Projects/

set -e

GITHUB_ORG="ningyos"
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

REPOS=(
    "DollOS"
    "DollOS-Android"
    "DollOS-Server"
    "DollOSAIService"
    "DollOSService"
    "DollOSSetupWizard"
    "DollOSLauncher"
    "device_dollos_bluejay"
    "vendor_dollos"
    "fish-tts"
    "luxtts-onnx"
    "tuna"
)

echo "=== DollOS Sync ==="
echo "Base directory: $BASE_DIR"
echo ""

for repo in "${REPOS[@]}"; do
    dir="$BASE_DIR/$repo"
    if [ -d "$dir/.git" ]; then
        echo "[$repo] Pulling..."
        (cd "$dir" && git pull --ff-only 2>&1 | tail -1)
    elif [ -d "$dir" ]; then
        echo "[$repo] Directory exists but not a git repo, skipping"
    else
        echo "[$repo] Cloning..."
        git clone "https://github.com/$GITHUB_ORG/$repo.git" "$dir" 2>&1 | tail -1
    fi
done

echo ""
echo "=== Status ==="
for repo in "${REPOS[@]}"; do
    dir="$BASE_DIR/$repo"
    if [ -d "$dir/.git" ]; then
        branch=$(cd "$dir" && git branch --show-current 2>/dev/null || echo "?")
        changes=$(cd "$dir" && git status --porcelain 2>/dev/null | wc -l)
        if [ "$changes" -gt 0 ]; then
            echo "  $repo ($branch) — $changes uncommitted changes"
        else
            echo "  $repo ($branch) — clean"
        fi
    fi
done
