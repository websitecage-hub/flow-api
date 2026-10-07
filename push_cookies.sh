#!/usr/bin/env bash
# ONE COMMAND: push the live session cookies into the repo.
#
#   ./push_cookies.sh   (after `git add sessions/ && git commit`)
#
# REFUSES if the repo is public (live Google cookies = account takeover).
set -e
REPO=$(git config --get remote.origin.url)
TOKEN="${GH_PAT:-${GITHUB_TOKEN:-}}"
SLUG=$(echo "$REPO" | sed -E 's#.*github.com[:/]##; s#\.git$##')
priv="unknown"
if [ -n "$TOKEN" ]; then
  priv=$(curl -sS -H "Authorization: Bearer $TOKEN" \
         "https://api.github.com/repos/$SLUG" \
         | python3 -c "import sys,json;print(json.load(sys.stdin).get('private'))" 2>/dev/null || echo unknown)
fi
if [ "$priv" = "False" ]; then
  echo "REFUSING: $SLUG is PUBLIC. Make it private first (Settings -> Danger Zone)."
  echo "Live cookies on a public repo let anyone log into the Google account."
  exit 1
fi
echo "repo $SLUG private=$priv -> pushing cookies"
git add -f sessions/flow.cookies.full.json
git commit -q -m "session cookies (private repo) — for FLOW_COOKIES_JSON" || echo "(nothing to commit)"
git push origin HEAD
echo "done. Set FLOW_COOKIES_JSON on Render from sessions/flow.cookies.full.json"