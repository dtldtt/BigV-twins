# BigV-twins OpenClaw Skills

One Skill per blogger. Each one is a thin behavioral wrapper around the
**bigv-twins** MCP server (running on the same host at `127.0.0.1:8770/mcp`).

Skill bodies deliberately do **not** embed the persona text. They tell the
agent to call `bigv-twins.get_persona` at runtime, so persona updates take
effect immediately without re-installing the skill.

## Files

```
skills/
├── README.md              ← this file
├── bigv-mr-dang/SKILL.md
├── bigv-eyu/SKILL.md
├── bigv-sanren/SKILL.md
└── bigv-shen/SKILL.md
```

Regenerate all four from the shared template (`scripts/generate_skills.py`):

```bash
python scripts/generate_skills.py             # all
python scripts/generate_skills.py --blogger eyu
```

## Installing into OpenClaw

`openclaw skills install <slug>` only works for skills published on ClawHub.
For local development we **copy** the skill folders directly into the agent's
workspace:

```bash
for slug in mr-dang eyu sanren shen; do
  rm -rf ~/.openclaw/workspace/skills/bigv-$slug
  cp -r skills/bigv-$slug ~/.openclaw/workspace/skills/
done
```

OpenClaw picks up the new skills automatically (no restart needed). Verify:

```bash
openclaw skills list | grep bigv     # should show 4 ✓ ready lines
```

(`deploy.sh` does all of this automatically.)

## Note about symlinks

OpenClaw's skills loader skips any skill path that resolves *outside* the
configured workspace skills root via symlink (`reason=symlink-escape`). So you
**cannot** symlink the project's `skills/` folder into the workspace — you have
to copy. Re-copy after editing.
