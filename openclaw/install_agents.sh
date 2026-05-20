#!/usr/bin/env bash
# install_agents.sh — provision the bigv + advisor OpenClaw agents on a fresh machine.
#
# Run after `openclaw setup` has succeeded once. Idempotent: re-running just
# refreshes the IDENTITY/SOUL/AGENTS markdown in each workspace.
#
# Tools used:
#   openclaw agents add <id> --workspace <path> --non-interactive
#   openclaw mcp servers set <id> ...
#
# This file lives in the BigV-twins git repo; copy it onto the target host
# under the project root and run from there.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCLAW_DIR="${OPENCLAW_DIR:-$HOME/.openclaw}"

# Locate openclaw binary (installed under nvm)
if command -v openclaw >/dev/null 2>&1; then
  OC=openclaw
else
  OC="$(find "$HOME/.nvm" -maxdepth 5 -name openclaw -type f 2>/dev/null | head -n1)"
  if [[ -z "$OC" ]]; then
    echo "ERROR: openclaw command not found. Install via npm install -g openclaw, then re-run." >&2
    exit 1
  fi
fi
echo "Using openclaw binary: $OC"

# ------------------------------------------------------------ bigv
BIGV_WS="$OPENCLAW_DIR/workspace-bigv"
echo "==> Provisioning bigv agent (workspace: $BIGV_WS)"
"$OC" agents add bigv --workspace "$BIGV_WS" --non-interactive || true

cp "$PROJECT_ROOT/openclaw/agents/bigv/IDENTITY.md" "$BIGV_WS/IDENTITY.md"
cp "$PROJECT_ROOT/openclaw/agents/bigv/SOUL.md"     "$BIGV_WS/SOUL.md"
cp "$PROJECT_ROOT/openclaw/agents/bigv/AGENTS.md"   "$BIGV_WS/AGENTS.md"
echo "    bigv IDENTITY/SOUL/AGENTS installed."

# ------------------------------------------------------------ advisor
ADV_WS="$OPENCLAW_DIR/workspace-advisor"
echo "==> Provisioning advisor agent (workspace: $ADV_WS)"
"$OC" agents add advisor --workspace "$ADV_WS" --non-interactive || true

cp "$PROJECT_ROOT/openclaw/agents/advisor/IDENTITY.md" "$ADV_WS/IDENTITY.md"
cp "$PROJECT_ROOT/openclaw/agents/advisor/SOUL.md"     "$ADV_WS/SOUL.md"
cp "$PROJECT_ROOT/openclaw/agents/advisor/AGENTS.md"   "$ADV_WS/AGENTS.md"
echo "    advisor IDENTITY/SOUL/AGENTS installed."

# ------------------------------------------------------------ skills (advisor needs agent-browser)
ADV_SKILLS="$ADV_WS/skills"
mkdir -p "$ADV_SKILLS"
if [[ -d "$OPENCLAW_DIR/workspace/skills/agent-browser" && ! -d "$ADV_SKILLS/agent-browser" ]]; then
  cp -r "$OPENCLAW_DIR/workspace/skills/agent-browser" "$ADV_SKILLS/"
  echo "    advisor: copied agent-browser skill from main workspace."
elif [[ ! -d "$ADV_SKILLS/agent-browser" ]]; then
  echo "    WARNING: $OPENCLAW_DIR/workspace/skills/agent-browser not found."
  echo "      Run: $OC skills install agent-browser  (or copy from another workspace)."
fi

# ------------------------------------------------------------ MCP servers
echo "==> Registering MCP servers (bigv-blogger / bigv-market)"
"$OC" mcp servers set bigv-blogger --url http://127.0.0.1:8770/mcp --transport streamable-http || true
"$OC" mcp servers set bigv-market  --url http://127.0.0.1:8771/mcp --transport streamable-http || true

# ------------------------------------------------------------ remove deprecated
"$OC" mcp servers unset bigv-twins 2>/dev/null || true

echo
echo "Done. Verify with:"
echo "  $OC agents list"
echo "  $OC mcp servers list"
echo
echo "Both MCP servers should be reachable on 127.0.0.1:8770 (blogger) and 127.0.0.1:8771 (market)."
echo "Start them with:  systemctl --user start bigv-twins-blogger.service bigv-twins-market.service"
