# `openclaw/` — OpenClaw agent configs (version-controlled)

This directory keeps the IDENTITY/SOUL/AGENTS markdown for every OpenClaw agent
that BigV-twins relies on, **outside** of `~/.openclaw/` so they can travel
with the repo and stay reviewable.

## Structure

```
openclaw/
├── README.md              ← this file
├── install_agents.sh      ← provisioning script (idempotent)
└── agents/
    ├── bigv/              ← role-play executor for the 5 archived bloggers
    │   ├── IDENTITY.md
    │   ├── SOUL.md
    │   └── AGENTS.md
    └── advisor/           ← AI investment advisor (control group, no blogger corpus)
        ├── IDENTITY.md
        ├── SOUL.md
        └── AGENTS.md
```

## Quick install (fresh machine)

```bash
cd /path/to/BigV-twins
bash openclaw/install_agents.sh
```

The script:

1. Runs `openclaw agents add bigv --workspace ~/.openclaw/workspace-bigv --non-interactive`
2. Copies the three `bigv/*.md` files into that workspace, overriding the default
   "small disciple" identity.
3. Same for `advisor` → `~/.openclaw/workspace-advisor/`.
4. Copies the `agent-browser` skill into the advisor workspace (so the advisor
   can do web search; the bloggers don't get it).
5. Registers the two split MCP servers via `openclaw mcp servers set` —
   `bigv-blogger` (http://127.0.0.1:8770/mcp) and `bigv-market` (http://127.0.0.1:8771/mcp).

## Agent design summary

| Agent     | Workspace                          | What it does                                 | MCP whitelist                  | Skills           |
| --------- | ---------------------------------- | -------------------------------------------- | ------------------------------ | ---------------- |
| `bigv`    | `~/.openclaw/workspace-bigv`       | Role-plays one of the 5 bloggers per request | `bigv-blogger.*` + `bigv-market.*` | (none required)  |
| `advisor` | `~/.openclaw/workspace-advisor`    | Neutral third-party market analyst           | `bigv-market.*` only           | `agent-browser`  |

`bigv-blogger.*` is **explicitly forbidden** for advisor (enforced via prompt;
OpenClaw doesn't filter MCP tools per-agent at config level yet).

## Why this is in git

- **Reproducibility**: the per-agent identity is part of the product, not a
  local quirk. A fresh deploy must produce the same role-play behavior.
- **Reviewability**: changes to "who the agent is" go through the same code
  review as Python changes.
- **Drift detection**: if someone hand-edits the workspace identity files on
  the running host, you can `diff` them against this directory.

## Re-syncing after manual edits

If you tweak `~/.openclaw/workspace-bigv/AGENTS.md` directly to test something,
copy the final version back into `openclaw/agents/bigv/AGENTS.md` so the repo
stays the source of truth, then commit.
