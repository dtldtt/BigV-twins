# BigV-twins

> 投资博主的"数字分身" —— 把知乎归档变成可对话的 RAG 知识库，挂在 OpenClaw agent 上随时问；外加一位中立 AI 投顾做对照组。

5 位归档博主 = 每位一个 [Skill](https://docs.openclaw.ai/clawhub/skill-format.md) + 一份 [persona 摘要](personas/) + 一份 [向量化的归档](twins/) + 共享的两个 MCP server（博主语料 + 市场数据）。专门的 `bigv` agent 收到问题 → 调 MCP server 读 persona / 检索语料 / 拿真实行情 → 用 OpenClaw 配的 LLM 生成带引文的第一人称回答。

旁边还挂了一个 `advisor` agent —— **不**接博主语料，只用市场数据 + web 搜索做中立分析，作为「对照组」。详见 [§17 多 Agent 架构](#17-多-agent-架构bigv-博主分身--advisor-投顾对照组)。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 工作原理](#2-工作原理)
- [3. 目录结构](#3-目录结构)
- [4. 前置依赖](#4-前置依赖)
- [5. 一键部署](#5-一键部署)
- [6. 部署脚本做了什么（逐步解释）](#6-部署脚本做了什么逐步解释)
- [7. 日常使用](#7-日常使用)
- [8. 自动维护机制](#8-自动维护机制)
- [9. 添加一个新博主](#9-添加一个新博主)
- [10. 刷新 persona](#10-刷新-persona)
- [11. 迁移到新机器](#11-迁移到新机器)
- [12. 故障排查](#12-故障排查)
- [13. 文件清单](#13-文件清单)
- [14. 赛博大V Web UI（可选）](#14-赛博大v-web-ui可选)
  - [14.10 HTTPS（推荐：Caddy + nip.io）](#1410-https推荐caddy--nipio无需买域名)
- [15. 股票基本面 MCP 工具](#15-股票基本面-mcp-工具)
- [16. 主题市场上下文（topics.json）](#16-主题市场上下文topicsjson)
- [17. 多 Agent 架构（bigv 博主分身 + advisor 投顾对照组）](#17-多-agent-架构bigv-博主分身--advisor-投顾对照组)
- [18. MCP Server 构建 / 部署速查](#18-mcp-server-构建--部署速查commit-拆分总览)

---

## 1. 这是什么

一个**本地 RAG 系统 + MCP 工具服务器**，让你能用自然语言询问任意被归档的投资博主：

> 「MR Dang 怎么看 A 股市场？」
>
> 「鳄鱼最近在说什么？」
>
> 「沈同学的投资框架是什么？」

Agent 返回的回答**严格基于博主原文**，每条观点附知乎原文链接。**不会编造**博主从未表达过的观点（找不到时直接说找不到）。

### 解决什么问题

- 你已经在 `/home/dtl/projects/zhihu` 归档了一批投资博主的内容（防 404 / 删档）
- 内容很多，单纯翻阅找不到想要的观点
- 想用 AI 帮你"调用"这些归档，但不希望被通用 AI 的训练知识污染、也不希望它编造
- 你已经在用 OpenClaw 作为统一的 agent 入口（微信 / Telegram / CLI 都可），希望复用它

---

## 2. 工作原理

### 总体架构

```
┌────────────────────── 你这边（任意聊天入口） ──────────────────────┐
│                                                                  │
│   微信 / Telegram / openclaw agent CLI                            │
│                       │                                          │
│                       ▼                                          │
│             OpenClaw agent (默认: main)                           │
│                       │                                          │
│                       │ 触发对应 skill (bigv-mr-dang/eyu/...)     │
│                       │ skill 指示 agent 调 MCP 工具              │
│                       ▼                                          │
└──────────────── MCP 协议 (streamable-http) ──────────────────────┘
                       │
                       ▼
┌─────── BigV-twins MCP Server (127.0.0.1:8770, systemd 常驻) ──────┐
│                                                                  │
│   tools:                                                         │
│     • list_bloggers()                                            │
│     • get_persona(blogger)        → personas/{slug}.md           │
│     • search(blogger, query, k)   → 向量检索 twins/{slug}.db     │
│     • get_recent(blogger, n)      → 读 zhihu.db                  │
│     • get_post(blogger, zhihu_id) → 读 zhihu.db 单篇全文           │
│     • get_stock_snapshot(query)   → 拉股票基本面+大盘 (见 §15)    │
│                                                                  │
│   resource:                                                      │
│     • persona://blogger/{slug}                                   │
│                                                                  │
│   ⚠ 此 server 不调任何 LLM，纯数据 + 检索                          │
└──────────────────────┬──────────────────┬────────────────────────┘
                       │ 只读              │ 读 / 写
                       ▼                  ▼
   /home/dtl/projects/zhihu/         twins/{slug}.db
   data/zhihu.db                     (sqlite + sqlite-vec
   (你的归档项目)                     · BGE 向量 · 元数据)
                       ▲
                       │ 每日 03:25 systemd timer
                       │
              增量索引器（scripts 触发 src/bigv_twins/index.py）
              扫 zhihu.db 中 created_at > last_indexed_at 的新行 →
              chunk → BGE embed → 写入对应 twins/*.db
```

### "训练"到底训练了什么

**严格意义上没有训练任何模型。** 用的是 RAG（检索增强生成），流程是：

| 步骤 | 做什么 | 用了什么 |
|---|---|---|
| **索引时（offline）** | 把每篇博客切成 ~600 字的 chunk，每个 chunk 用 BGE-base-zh-v1.5 算向量，存进 sqlite-vec | BGE-base-zh-v1.5（预训练好的开源中文 embedding 模型，不修改） |
| **查询时（online）** | 把用户问题用同一个 BGE 算向量，在 sqlite-vec 里找最近邻的 chunks 返回 | 同上 |
| **生成回答** | Agent 拿到检索 chunks + persona，让 LLM 整合成自然语言回答 | OpenClaw 配的 LLM（当前是 Bailian/Qwen 3.5 Plus） |
| **persona 生成（一次性）** | 让 LLM 读每个博主前 30 篇高赞总结成 1-2KB 风格指南 | OpenClaw 配的 LLM（同上） |

所以"训练好的数据"是 `twins/{slug}.db` —— 不是模型权重，而是**预算好的向量索引**。模型 BGE 始终是同一份预训练版本。

---

## 3. 目录结构

```
BigV-twins/
├── README.md                       ← 你在看
├── deploy.sh                       ← 一键部署（见 §5）
├── pyproject.toml                  ← Python 依赖清单
├── .env.example                    ← 配置模板（部署时拷成 .env）
├── .gitignore
│
├── src/bigv_twins/                 ← Python 包源码
│   ├── __init__.py                 (load_dotenv + 导出 settings)
│   ├── config.py                   (从 bloggers.json 加载博主 + Settings)
│   ├── chunk.py                    (HTML 剥离 + 段落滑窗切分)
│   ├── embed.py                    (sentence-transformers + BGE 封装)
│   ├── index.py                    (增量索引器；CLI: python -m bigv_twins.index)
│   ├── search.py                   (sqlite-vec 检索；CLI: python -m bigv_twins.search)
│   ├── blogger_server.py           (FastMCP「博主语料」server，port 8770；见 §17)
│   ├── market_server.py            (FastMCP「市场数据」server，port 8771；见 §17)
│   ├── stock_data.py               (股票快照：Tencent + akshare 多源组合；见 §15)
│   ├── market_data.py              (主题市场上下文 + topics.json 加载；见 §16)
│   └── web/                        ← 赛博大V Web UI（FastAPI；见 §14）
│       ├── app.py / db.py / auth.py / auth_routes.py / invites.py
│       ├── chat.py / admin.py / about.py / openclaw_client.py / bootstrap.py
│       ├── templates/{base,login,register,placeholder,chat/*,admin/*,about/*}.html
│       └── static/{style.css, chat.js}
│
├── bloggers.json                   ← 博主元数据（slug / author_id / url_token / name / tagline / kind / agent）
│
├── openclaw/                       ← OpenClaw agent 配置（版本控制；见 §17）
│   ├── README.md
│   ├── install_agents.sh           (provision bigv + advisor 的幂等脚本)
│   └── agents/
│       ├── bigv/{IDENTITY,SOUL,AGENTS}.md       (博主分身专用)
│       └── advisor/{IDENTITY,SOUL,AGENTS}.md    (AI 投顾对照组)
│
├── scripts/
│   ├── add_blogger.py              ← 一键添加新博主（更新 json + 索引 + persona + skill + 复制）
│   ├── generate_personas.py        ← persona 生成（分层采样 + 可选 verify 自校）
│   ├── generate_skills.py          ← 模板化生成 5 个 SKILL.md
│   └── test_mcp_client.py          ← MCP 服务端到端冒烟测试
│
├── systemd/                        ← systemd 单元的源文件 + 安装脚本
│   ├── bigv-twins-blogger.service  (博主语料 MCP server，port 8770)
│   ├── bigv-twins-market.service   (市场数据 MCP server，port 8771)
│   ├── bigv-twins-daily.service    (每日增量任务)
│   ├── bigv-twins-daily.timer      (定时器：每天 03:17 + 抖动)
│   ├── bigv-twins-web.service      (web chat UI 常驻；见 §14)
│   └── install_systemd.sh          (复制到 ~/.config/systemd/user/ 并 enable)
│
├── skills/                         ← 「真相」：5 个博主 OpenClaw Skill 的源
│   ├── README.md
│   ├── bigv-mr-dang/SKILL.md
│   ├── bigv-eyu/SKILL.md
│   ├── bigv-sanren/SKILL.md
│   ├── bigv-shen/SKILL.md
│   └── bigv-paipi/SKILL.md
│
├── personas/                       ← 生成出来的风格指南（小，进仓库）
│   ├── mr-dang.md / eyu.md / sanren.md / shen.md / paipi.md
│
├── twins/                          ← 向量化好的 RAG 数据库（每博主一个 .db）
│   ├── mr-dang.db / eyu.db / sanren.db / shen.db / paipi.db
│   ⚠ 默认 gitignore；迁移时可单独 rsync 节省重建时间
│
├── deploy/                         ← 运维模板
│   └── Caddyfile.example           (反代 + 自动 HTTPS；见 §14.10)
│
├── logs/                           ← 索引器 + MCP server + web 日志（gitignored）
│   ├── bootstrap.log
│   ├── mcp_blogger.log / mcp_market.log
│   ├── daily_index.log
│   └── web.log
│
└── chats.db                        ← 仅当启用 web UI 时存在（用户 / 邀请 / 对话；gitignored）
```

### 部署后的「副本」（不在项目里）

| 路径 | 内容 | 关系 |
|---|---|---|
| `~/.config/systemd/user/bigv-twins-*.{service,timer}` | systemd 单元 | 由 `systemd/install_systemd.sh` 复制 |
| `~/.openclaw/workspace-bigv/skills/bigv-*/` | 5 个博主 SKILL.md | 由 `deploy.sh` 从 `skills/` 复制 |
| `~/.openclaw/workspace-bigv/{IDENTITY,SOUL,AGENTS}.md` | bigv agent 身份定义 | 由 `openclaw/install_agents.sh` 写入 |
| `~/.openclaw/workspace-advisor/{IDENTITY,SOUL,AGENTS}.md` | advisor agent 身份定义 | 同上 |
| `~/.openclaw/workspace-advisor/skills/agent-browser/` | advisor 的 web 搜索能力 | 同上（从 main workspace 复制） |
| `~/.openclaw/openclaw.json` 里的 `mcp.servers.{bigv-blogger,bigv-market}` | MCP 连接配置 | 由 `openclaw mcp servers set` 写入 |
| `~/.cache/huggingface/...` | BGE-base-zh-v1.5 模型权重（~400 MB） | 首次 embed 时自动下载 |

更新 `skills/` 后**必须**重新复制到 OpenClaw workspace（见 §9）；
更新 `openclaw/agents/*/` 后跑 `bash openclaw/install_agents.sh` 同步（见 §17）。

---

## 4. 前置依赖

### 4.1 外部依赖（必须自己准备）

| # | 依赖 | 为什么需要 | 怎么验证 |
|---|---|---|---|
| 1 | **知乎归档项目** + 可读的 sqlite db | 数据源；本项目以 `?mode=ro&immutable=1` 只读 | `ls /home/dtl/projects/zhihu/data/zhihu.db` |
| 2 | **OpenClaw** 已安装且 gateway 在运行，并已配置至少一个 LLM provider | Agent runtime + 生成 persona 时用的 LLM | `openclaw status` 看到 gateway active；`openclaw agents list` 看到至少一个 agent，有 model 字段 |
| 3 | **conda / miniconda** | 隔离 Python 环境（部署脚本会建一个 `bigv-twins` env） | `which conda` |
| 4 | **systemd**（用户级即可） | 让 MCP server 常驻 + 每日定时任务 | `systemctl --user status` |
| 5 | **网络**：能访问 `hf-mirror.com` 或 `huggingface.co` | 首次下载 BGE 模型（~400 MB）；之后不再需要 | `curl -sI https://hf-mirror.com` |

### 4.2 OpenClaw 该配成什么样

部署脚本会**读** `~/.openclaw/openclaw.json` 自动发现 provider 配置。最低要求：

```jsonc
// ~/.openclaw/openclaw.json
{
  "gateway": { /* ... */ },
  "models": {
    "providers": {
      "<provider-name>": {                    // 比如 "bailian"、"openai"、"anthropic"
        "baseUrl": "https://.../v1",          // OpenAI 兼容 endpoint
        "apiKey": "sk-...",
        "api": "openai-completions",
        "models": [{ "id": "model-name", /* ... */ }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "<provider-name>/<model-id>" }  // 必须有 primary
    }
  }
}
```

如果 OpenClaw 没装/没配，部署脚本仍能完成索引和 MCP server 部署，只是 persona 生成和 skill 注册会跳过（你之后手动跑）。

### 4.3 硬件参考

- **CPU**：4 核够用（索引器满载约 200% CPU）
- **RAM**：7 GB 是下限（BGE-base-zh-v1.5 推理峰值约 600 MB；如有 16 GB+ 可换成更强的 BGE-large 或 BGE-M3）
- **磁盘**：项目本身 < 1 GB；HF 模型缓存约 400 MB；torch 装好后约 3 GB；twins 数据每博主 ~5 MB–~300 MB（看体量）
- **GPU**：不需要

---

## 5. 一键部署

### 最小调用

```bash
git clone <your-repo>/BigV-twins.git
cd BigV-twins
./deploy.sh --zhihu-db /home/dtl/projects/zhihu/data/zhihu.db
```

### 所有参数

```bash
./deploy.sh \
  --zhihu-db /path/to/zhihu.db              # 必填：知乎归档 db 路径
  [--conda-env bigv-twins]                  # 默认 bigv-twins
  [--mcp-port 8770]                         # 默认 8770
  [--skip-index]                            # 跳过首次全量索引（如果 twins/ 里已有 db）
  [--skip-personas]                         # 跳过 persona 生成（如果 personas/ 里已有，会自动跳过）
  [--skip-systemd]                          # 跳过 systemd 安装
  [--skip-openclaw]                         # 跳过 openclaw 集成（注册 MCP + 复制 skills）
  [--force-personas]                        # 强制重新生成 persona（覆盖现有）
```

### 完成后验证

部署脚本最后会自动跑冒烟测试。手动验证：

```bash
# 1. MCP server 在跑
systemctl --user status bigv-twins-server

# 2. 4 个博主的 twin db 都有
ls -lh twins/

# 3. agent 能用
openclaw agent --agent main -m "MR Dang 怎么看 A 股市场？" --json | jq -r .result.finalAssistantVisibleText
```

---

## 6. 部署脚本做了什么（逐步解释）

每一步对应 `deploy.sh` 里的一个函数，便于排查 / 单独重跑。

### Step 1: 前置检查 `check_prereqs`

- conda 在 PATH 里
- 知乎 db 路径存在且可读
- 检测 openclaw CLI（PATH 或 `~/.nvm/versions/node/*/bin/openclaw`）— 找不到只警告，不退出

### Step 2: conda 环境 `ensure_conda_env`

```bash
conda create -y -n bigv-twins python=3.12
```

幂等：已有就跳过。

### Step 3: 安装 Python 包 `install_package`

```bash
conda activate bigv-twins
pip install -e .              # 读 pyproject.toml，装 sentence-transformers / mcp / sqlite-vec / openai / ...
```

约 1–3 分钟（torch 包大）。

### Step 4: 写入 `.env` `generate_env_file`

从 `.env.example` 复制为 `.env`，填入：
- `ZHIHU_DB_PATH`（来自 --zhihu-db 参数）
- `TWINS_DIR`、`PERSONAS_DIR`（项目内绝对路径）
- `MCP_PORT`（来自 --mcp-port）
- `HF_ENDPOINT=https://hf-mirror.com`（国内默认）

### Step 5: 预下载 embedding 模型 `pre_download_model`

```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5')"
```

避免首次索引时被 ~400 MB 下载惊到。

### Step 6: 首次全量索引 `bootstrap_index`

```bash
python -m bigv_twins.index            # 跑全部博主；按 zhihu_id 幂等
```

- 4 个博主当前数据量预计耗时：mr-dang 5min / eyu 4min / sanren 35min / shen 4-6 hours
- 中途可断；重跑会跳过已索引行
- 加 `--skip-index` 跳过（如果你 rsync 了别人的 twins/*.db 过来）

### Step 7: 生成 persona `generate_personas`

```bash
python scripts/generate_personas.py
```

读 `~/.openclaw/openclaw.json` 拿 provider 配置，调它生成 4 个 persona。每博主 60–90 秒，~$0（走 OpenClaw 已有 provider 的额度）。

幂等：默认跳过已有的 `personas/{slug}.md`，加 `--force-personas` 强制覆盖。

如果 OpenClaw 未配置，本步骤 skip 并提示用户手动跑。

### Step 8: 装 systemd `install_systemd`

```bash
sudo loginctl enable-linger $USER       # 让用户级 systemd 跨登出仍跑
cp systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bigv-twins-server.service
systemctl --user enable --now bigv-twins-daily.timer
```

完成后：
- MCP server 常驻 127.0.0.1:`MCP_PORT`
- 每天 `03:17 + 抖动 10min` 跑增量索引

### Step 9: 注册 MCP server 到 OpenClaw `register_mcp`

```bash
openclaw mcp set bigv-twins '{"url":"http://127.0.0.1:8770/mcp","transport":"streamable-http"}'
```

幂等。openclaw 会自动加到 `~/.openclaw/openclaw.json` 的 `mcp.servers`。

### Step 10: 复制 skills 到 OpenClaw workspace `install_skills`

```bash
for slug in mr-dang eyu sanren shen; do
  rm -rf ~/.openclaw/workspace/skills/bigv-$slug
  cp -r skills/bigv-$slug ~/.openclaw/workspace/skills/
done
```

OpenClaw 自动 pick up，几秒内 `openclaw skills list` 就能看到。

### Step 11: 冒烟测试 `smoke_test`

```bash
python scripts/test_mcp_client.py
```

通过 MCP 协议连本地 server，列工具、列博主、对 eyu 做一次 search、读一个 persona。任何失败会高亮提示。

---

## 7. 日常使用

### 提问

```bash
# 在服务器上（最直接）
openclaw agent --agent main -m "MR Dang 怎么看 A 股市场？"
openclaw agent --agent main -m "鳄鱼对房地产怎么看？"
openclaw agent --agent main -m "三人禾最近在聊什么？"

# 已配通的话直接通过微信/Telegram/...聊天，触发关键词同样生效
```

### 关键词 → 触发哪个 skill

每个 skill 在 SKILL.md 里声明了触发别名（看 `skills/bigv-*/SKILL.md` 顶部）：

| Skill | 触发关键词（部分） |
|---|---|
| bigv-mr-dang | MR Dang、Dang、dang 哥、当哥 |
| bigv-eyu | 鳄鱼、寒武纪、寒武纪鳄鱼、寒武纪的鳄鱼 |
| bigv-sanren | 三人禾、三人、水又三人禾 |
| bigv-shen | 沈同学、沈、阳光下沈、阳光下的沈同学 |

### 直接用 CLI 查检索（不走 agent）

```bash
conda activate bigv-twins && cd /path/to/BigV-twins
python -m bigv_twins.search --blogger eyu --query "对房地产的看法" --top-k 5
```

---

## 8. 自动维护机制

```
zhihu 爬虫（你的）── 每天爬新内容到 zhihu.db
                              │
                              ▼ 03:17 + 抖动
              bigv-twins-daily.timer 触发
                              │
                              ▼
              bigv-twins-daily.service:
              python -m bigv_twins.index
                  │
                  └─ 扫所有博主: 新行 / hash 变了的行 → re-embed → 写 twins/*.db
```

- 增量逻辑：每行的 `sha1(content || updated_time)` 存进 `indexed_contents` 表，不变就跳过
- 日志：`logs/daily_index.log`（append）
- 失败不影响 MCP server 在跑

### 查看下次运行

```bash
systemctl --user list-timers --all | grep bigv-twins
```

### 手动跑一次增量

```bash
systemctl --user start bigv-twins-daily.service
journalctl --user -u bigv-twins-daily.service -n 50
```

### MCP server 出问题怎么办

```bash
systemctl --user status bigv-twins-server      # 看状态
journalctl --user -u bigv-twins-server -n 100  # 看日志
systemctl --user restart bigv-twins-server     # 重启
```

---

## 9. 添加一个新博主

**前提**：zhihu 项目那边已经把这个博主加进爬虫并跑了至少一轮，所以 `zhihu.db.authors`
里有这个人。你需要知道两件事：

| 你提供 | 说明 |
|---|---|
| `--author-id N` | 该博主在 `zhihu.db.authors` 里的 id（`sqlite3 zhihu.db "select * from authors"` 看一眼） |
| `--slug` | 你想给他起的短名（小写字母 / 数字 / `-`），如 `mr-dang` / `newbie`。会出现在 URL 和 skill 名里 |
| `--tagline`（可选但强烈推荐） | 一句话简介，会显示在 `/chat` 卡片上 |

**一条命令搞定**：

```bash
conda activate bigv-twins && cd /path/to/BigV-twins
python scripts/add_blogger.py \
  --author-id 5 \
  --slug newbie \
  --tagline "趋势交易 · 重视成交量" \
  [--name "新博主"]    # 可选，默认从 zhihu.db 取
```

脚本自动做的事：
1. 验证 author_id 在 zhihu.db 存在；读 name + url_token
2. 追加到 `bloggers.json`（in-process settings 缓存的需要 web 重启才生效）
3. 跑全量索引（新博主第一次会跑几分钟到几小时，看体量）
4. 用 OpenClaw 配的 LLM 生成 persona
5. 渲染 SKILL.md
6. 复制到 `~/.openclaw/workspace/skills/bigv-{slug}/`

跑完一条提示：

```bash
systemctl --user restart bigv-twins-web    # 让 web 卡片显示新博主
# 在 OpenClaw agent 测试：
openclaw agent --agent main -m "新博主 你怎么看 X"
```

MCP server **不用重启**——它每次请求都重新打开对应 .db 文件。

每个 `--skip-*` 阶段都能单独跳过（比如已经索引过、只想重生 persona）。

---

## 10. 刷新 persona

Persona 会随博主风格 drift 而变得不准。建议每 1–2 月或大事件后刷新一次。

### 简单刷新（默认）— 分层采样

```bash
conda activate bigv-twins && cd /path/to/BigV-twins
python scripts/generate_personas.py --force                # 全部 4 个
python scripts/generate_personas.py --blogger eyu --force  # 只刷新 eyu
```

默认采样：**高赞 20 + 最近 10 + 长文章 10 = 40 篇去重后**喂给 LLM 总结。
比之前的"纯 top 30 高赞"覆盖更全（捕获近期风格 + 详细长文）。

成本：每个博主一次 LLM 调用，~ ¥0.5–1（走你 OpenClaw 配的 provider，目前是 Bailian/Qwen）。

### 高质量刷新 — verify 自校

```bash
python scripts/generate_personas.py --force --verify              # 全部刷新（带 verify）
python scripts/generate_personas.py --blogger shen --force --verify
```

加 `--verify` 启动**两轮调用**：
1. 第一轮：基于训练样本生成 persona v1
2. 第二轮：拿**另外 10 篇没见过的代表作**给模型，让它对照 v1 找出缺失 / 偏差 / 矛盾，输出修订版

成本翻倍（~ ¥1–2/博主），换来更稳的画像。建议每季度跑一次 `--verify`，平时跑无 `--verify` 的版本。

### 其他参数

```bash
python scripts/generate_personas.py --help

# 调采样比例（默认 20/10/10，总共 40）
--top-voteup 30 --recent 15 --long 10

# 走旧的简单逻辑（纯 top-N 高赞）
--simple --top-n 30

# 看会采样什么但不调 API
--dry-run
```

改完不需要重启或重装 skill —— skill 在运行时调 `bigv-twins.get_persona`，永远读最新文件。

---

## 11. 迁移到新机器

### 方案 A：完整迁移（快，省去重建索引）

```bash
# 在旧机器上
cd /home/dtl/projects/BigV-twins
tar czf /tmp/bigv-twins.tar.gz \
  --exclude=logs --exclude=__pycache__ --exclude=.env \
  .                                                       # 包含 twins/、personas/、skills/、源码

# 把 HF 模型缓存也带过去（省 400MB 下载）
tar czf /tmp/hf-cache.tar.gz -C ~/.cache huggingface/hub/models--BAAI--bge-base-zh-v1.5

# 传到新机器
scp /tmp/bigv-twins.tar.gz /tmp/hf-cache.tar.gz user@new-host:/tmp/

# 在新机器上
mkdir -p ~/projects && cd ~/projects
tar xzf /tmp/bigv-twins.tar.gz -C ./BigV-twins
mkdir -p ~/.cache && tar xzf /tmp/hf-cache.tar.gz -C ~/.cache

cd BigV-twins
./deploy.sh --zhihu-db /new/path/to/zhihu.db --skip-index --skip-personas
```

**前提**：新机器有 conda + OpenClaw + 知乎归档 db 已就位。

### 方案 B：纯净迁移（从源码 + 数据重建）

```bash
# 旧机器：只打包源码 + persona + skill（不带 twins/）
cd /home/dtl/projects/BigV-twins
tar czf /tmp/bigv-twins-clean.tar.gz \
  --exclude=logs --exclude=twins --exclude=__pycache__ --exclude=.env \
  .

# 新机器
cd ~/projects
tar xzf /tmp/bigv-twins-clean.tar.gz -C ./BigV-twins
cd BigV-twins
./deploy.sh --zhihu-db /new/path/to/zhihu.db
# 注意：bootstrap_index 会花 4-6 小时（取决于博主体量）
```

---

## 12. 故障排查

### MCP server 不接受连接

```bash
# 端口被占？
ss -tlnp | grep 8770
# 服务挂了？
systemctl --user status bigv-twins-server
journalctl --user -u bigv-twins-server -n 50
# 模型加载失败？通常是网络问题
grep -i error logs/mcp_server.log
```

### Agent 说找不到工具 / skill 没触发

```bash
# OpenClaw 看到 MCP 了吗？
openclaw mcp list                                        # 应该有 bigv-twins
# Skill 装上了吗？
openclaw skills list | grep bigv                         # 应该 4 个，全 ✓ ready
# 工具暴露了吗？
python scripts/test_mcp_client.py                        # 列出 5 个 bigv-twins__* 工具
```

### 检索质量差

```bash
# 看看具体检索到了什么
python -m bigv_twins.search --blogger eyu --query "你的问题" --top-k 10
# distance > 1.1 通常说明语料里没相关内容
# distance < 0.95 是强相关
```

### 索引耗时长 / shen 一直跑不完

正常。BGE 在 4 核 CPU 上约 0.5–2 秒/条；shen 11k+ 条要 4-6 小时。可以放后台跑：

```bash
nohup python -m bigv_twins.index --blogger shen > logs/shen-rebuild.log 2>&1 &
tail -f logs/shen-rebuild.log | tr '\r' '\n'
```

### Persona 生成失败

```bash
# 通常因为 openclaw.json 里没配 provider
python3 -c "import json; d=json.load(open('/home/dtl/.openclaw/openclaw.json')); print(d.get('models',{}).get('providers'))"
# 应该至少有一个 provider，有 baseUrl + apiKey + models
```

### HF 模型下不下来

```bash
# 国内常用：换镜像
echo 'HF_ENDPOINT=https://hf-mirror.com' >> .env
# 或者代理
export HTTPS_PROXY=http://127.0.0.1:7890
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5')"
```

---

## 13. 文件清单

源代码（手动维护）：

| 文件 | 作用 | 何时改 |
|---|---|---|
| `src/bigv_twins/config.py` | 博主元信息 | 加新博主 |
| `src/bigv_twins/chunk.py` | HTML 清洗 + chunking | 改切分策略 |
| `src/bigv_twins/embed.py` | embedding 封装 | 换模型 |
| `src/bigv_twins/index.py` | 增量索引器 | 改 schema |
| `src/bigv_twins/search.py` | 检索 | 加过滤参数 |
| `src/bigv_twins/server.py` | MCP server | 加工具 |
| `scripts/generate_personas.py` | persona 生成 | 改 prompt |
| `scripts/generate_skills.py` | skill 模板渲染 | 改触发规则 / 输出格式 |
| `scripts/test_mcp_client.py` | 冒烟测试 | 加新工具的测试 |
| `systemd/*.service`/`.timer` | systemd 单元源 | 改运行参数 |
| `skills/bigv-*/SKILL.md` | skill 源（由 generate_skills.py 生成，可手工微调） | 改后必须 `cp` 到 OpenClaw workspace |
| `pyproject.toml` | Python 依赖 | 加 / 改包 |
| `.env.example` | 配置模板 | 加新环境变量 |
| `deploy.sh` | 一键部署 | 加新部署步骤 |

生成的产物（通常不手改）：

| 文件 | 作用 | 怎么重建 |
|---|---|---|
| `personas/{slug}.md` | LLM 生成的风格指南 | `python scripts/generate_personas.py --force` |
| `twins/{slug}.db` | sqlite + 向量索引 | `python -m bigv_twins.index --blogger {slug}` |
| `logs/*` | 运行日志 | 自动生成 |
| `.env` | 实际配置（不进 git） | 由 deploy.sh 生成 |

---

## 14. 赛博大V Web UI（可选）

如果想给受邀请的用户提供一个浏览器聊天界面（独立于 OpenClaw 的微信/Telegram 通道），开启 web UI。**不是必需的**——你完全可以只用 MCP + agent CLI / IM。

### 14.1 它是什么

一个独立的 FastAPI 应用监听 `127.0.0.1:8001`，提供：

- **登录 / 注册**（注册需邀请码；admin 在管理后台轮换邀请码）
- **博主 tab 列表**（自动过滤被 admin 隐藏的博主）
- **对话历史**（按博主分组，每个博主独立线程）
- **SSE 流式回复**（实时字符级吐字，比一次性返回好得多）
- **管理后台**（仅 admin）：仪表盘、邀请码管理、用户管理、博主显示控制、对话清理

数据存在项目根目录的 `chats.db`（独立于 `twins/*.db`，互不干扰）。

### 14.2 架构

```
浏览器 ←─SSE─ FastAPI :8001 ──HTTP──→ OpenClaw /v1/chat/completions :18789
                  │                              │
                  ▼                              ▼ (agent loop)
              chats.db (sqlite)              bigv-{slug} skill
              users / invites /                    │
              conversations / messages             ▼
                                            bigv-twins MCP :8770 (你已有的)
```

web app **不直接调** MCP server——它调 OpenClaw 的 `/v1/chat/completions`，让 OpenClaw 的 agent 触发对应 skill 后再去调 MCP。

### 14.3 部署

`./deploy.sh` 默认会一并部署（除非加 `--skip-web`）：

- 在 `.env` 里生成一个 32-byte `WEB_SECRET_KEY`（用于 cookie 签名）
- 启用 OpenClaw `/v1/chat/completions` 端点（写入 `~/.openclaw/openclaw.json`）+ 把 `agents.defaults.timeoutSeconds` 调到 180s
- 安装 `bigv-twins-web.service` user systemd unit
- 提示创建首个 admin 用户（用 `BIGV_ADMIN_USERNAME` / `BIGV_ADMIN_PASSWORD` 环境变量预设，或部署后手动跑 `python -m bigv_twins.web.bootstrap`）

> ⚠️ 首次启用 web 会触发 OpenClaw gateway 重启（~10s 不可用）。

### 14.4 创建首个 admin

```bash
conda activate bigv-twins && cd /path/to/BigV-twins
python -m bigv_twins.web.bootstrap        # 交互式：会要求 username + password（getpass）
# 或非交互：
BIGV_ADMIN_USERNAME=alice BIGV_ADMIN_PASSWORD=supersecret123 python -m bigv_twins.web.bootstrap
```

只能 bootstrap 一次（之后 admin 已存在会拒绝）。

### 14.5 给受邀请的人开账号

1. admin 登录 → 顶部 nav "管理" → "邀请码" → "生成新邀请码（作废旧的）"
2. 复制邀请码，发给朋友
3. 朋友访问 `/register`，填用户名 / 密码 / 邀请码 → 自动登录

⚠️ 同一时间只允许一个 active 邀请码。生成新的会作废旧的。已用旧码注册的账号**不受影响**。

### 14.6 把入口挂到你的 zhihu 站点

在 zhihu 项目的导航模板里加一行：

```html
<a href="http://你的域名:8001/" target="_blank">赛博大V</a>
```

仅此而已——chat 应用完全独立（独立 cookie / 独立数据库 / 独立账号体系）。

### 14.7 admin 能做的事

| 功能 | 在哪 |
|---|---|
| 看用户数 / 对话数 / 消息数 / token 用量 | `/admin` 仪表盘 |
| 轮换邀请码（旧码立即作废） | `/admin/invites` |
| 看每个用户的活跃度 + 删除非 admin 用户 | `/admin/users` |
| 隐藏 / 显示博主（前端立即生效，隐藏后历史也访问不到） | `/admin/bloggers` |
| 批量删除 N 天前未更新的对话 | `/admin/cleanup` |

### 14.8 故障排查

| 现象 | 排查 |
|---|---|
| `/login` 502/不响应 | `systemctl --user status bigv-twins-web` · `journalctl --user -u bigv-twins-web -n 100` |
| 发问后 `⚠ ...timeout` | OpenClaw `/v1/chat/completions` 没启用或 timeout 太短。看 `~/.openclaw/openclaw.json` 是否有 `gateway.http.endpoints.chatCompletions.enabled=true` 和 `agents.defaults.timeoutSeconds>=180` |
| 发问后立刻 `⚠ openclaw 401` | gateway token 变了。`tail ~/.openclaw/openclaw.json` 看新 token，web app 启动时缓存的会失效，restart `bigv-twins-web` |
| 注册失败"邀请码无效" | admin 是否已轮换 / 已生成；`select * from invites where deactivated_at is null` 看 active 那条 |

### 14.9 关掉 web

```bash
systemctl --user disable --now bigv-twins-web.service
# (可选) 把 openclaw 的 chatCompletions 端点关回去
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.openclaw' / 'openclaw.json'
d = json.loads(p.read_text())
d.get('gateway',{}).pop('http', None)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
"
```

`chats.db` 保留——下次启用还能继续。

### 14.10 HTTPS（推荐：Caddy + nip.io，无需买域名）

WEB_HOST 默认 `127.0.0.1`，即只本机可访问。要让外网用户安全访问（密码不明文传），强烈建议在前面套一层 Caddy 反代 + 自动 HTTPS。

#### 一次性安装

```bash
# 1. 在阿里云 ECS 安全组开放入方向 TCP 80 + 443（保持 8001 不开放）
# 2. 安装 Caddy（Debian / Ubuntu）
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# 3. 用项目里的模板（改成你自己的 hostname）
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo vim /etc/caddy/Caddyfile   # 把 8-155-174-112.nip.io 改成你 IP 对应的形式

# 4. reload；首次启动会自动向 Let's Encrypt 申请证书（~15s）
sudo systemctl reload caddy
sudo journalctl -u caddy -n 30
# 看到 "certificate obtained successfully" 就完事了
```

#### nip.io 域名格式

`nip.io` 是个公共 DNS，任何 `<ip>.nip.io` 自动解析到该 IP，无需注册：

- `1.2.3.4.nip.io`     → 1.2.3.4
- `1-2-3-4.nip.io`     → 1.2.3.4  （建议用这个，单层子域，Let's Encrypt 不会撞速率限制）

也可换成自己的域名——只要 DNS A 记录指向本机 IP，Caddyfile 里的 hostname 改成你的域名即可。

#### 改完后

- 把 `.env` 的 `WEB_HOST` 改回 `127.0.0.1` 然后 `systemctl --user restart bigv-twins-web`
- zhihu 站点 nav 里的链接改成 `https://你的域名.nip.io/`
- 验证：`curl -I https://你的域名.nip.io/login` 应返回 200

#### 证书续期

Caddy 自动管。到期前 ~30 天会自动续。`sudo journalctl -u caddy --since 24h | grep -i renew` 看续期记录。

---

## 15. 股票基本面 MCP 工具

`bigv-twins.get_stock_snapshot(query)` 让博主能拿到具体股票的**实时基本面**（PE/PB/市值/控股结构/主营业务/大盘最近 10 天行情），然后用自己的量化框架（"市值 < 200 亿"、"PE < 30"、"央企控股"等）对照真实数字给出判断，而不是只能用印象/常识。

### 15.1 为什么需要

博主框架里很多阈值是量化的，但 agent 在没数据时只能用常识猜（"科技股、可能民企、估计估值高"），这种笼统的判断说服力差。给它真数字 → agent 能逐条对照原则，**回答从"博主大概什么态度"变成"博主对这只股票的具体判断 + 量化理由 + 替代建议"**。我们做过 A/B 测试，差距明显。

### 15.2 数据源（容错组合）

| 数据 | 主源 | 备选 |
|---|---|---|
| 现价 / PE / PB / 市值 / 52w 高低 | Tencent `qt.gtimg.cn`（极稳定） | — |
| 主营业务 / 经营范围 | akshare 同花顺 `stock_zyjs_ths` | — |
| 实际控制人 / 控股性质 / 行业 | akshare 雪球 `stock_individual_basic_info_xq` | 重试 2 次 |
| 1 年涨跌幅 | akshare 新浪 `stock_zh_a_daily` | — |
| 大盘最近 10 天 | akshare 新浪 `stock_zh_index_daily` | — |
| 名称 ↔ 代码映射 | akshare `stock_info_a_code_name`（1h 缓存） | — |

每个数据源**独立 try/except**，单源失败不影响其他字段；快照按 ticker 10 分钟缓存。

⚠️ 东方财富的 `stock_zh_a_spot_em` / `stock_individual_info_em` 经常 503，**不作为主源**，仅在某些个例下用作兜底。

### 15.3 支持的市场

| 市场 | 名称识别 | 代码 | 现价/估值 | 主营 / 控股 / 行业 | 1y K 线 | 大盘 |
|---|---|---|---|---|---|---|
| **A 股主板** (60/00) | ✅ | ✅ | ✅ | ✅ | ✅ | 上证 |
| **创业板** (300/301) | ✅ | ✅ | ✅ | ✅ | ✅ | 上证 + 创业板指 |
| **科创板** (688) | ✅ | ✅ | ✅ | ✅ | ✅ | 上证 + 科创 50 |
| **北交所** (8xx) | ✅ | ✅ | ✅ | 部分 | ✅ | 上证 |
| **港股** (5 位数代码) | ❌（需直接给代码） | ✅ Tencent | ⚠️ 基础 | ❌ | ❌ | ❌（v2 加 HSI） |
| **美股** | ⚠️ 透传 ticker | — | ❌ | ❌ | ❌ | ❌ |

A 股是核心场景（这几个博主都聊 A 股），其他市场后续按需扩展。

### 15.4 输出长这样（实例）

```json
{
  "ok": true,
  "query": "茅台",
  "resolved": {"code":"600519","name":"贵州茅台","market":"a-share","board":"main"},
  "price": {"current":1324.3,"change_today_pct":0.10,"high_52w":1330,"low_52w":1318,"change_1y_pct":-11.4},
  "valuation": {"pe_ttm":20.05,"pb":6.12},
  "scale": {"total_market_cap_yi":16583.81,"total_market_cap_display":"1.66 万亿"},
  "ownership": {"actual_controller":"贵州省人民政府国有资产监督管理委员会 (48.96%)","ownership_class":"省属国资控股"},
  "business": {"main_business":"茅台酒及系列酒的生产与销售。","industry":"白酒"},
  "company": {"full_name":"贵州茅台酒股份有限公司","chairman":"陈华","staff_num":34992},
  "index_context": [
    {"name":"上证指数","recent_10d":[{"date":"2026-05-06","close":4160,"change_pct":-0.04}, ...]}
  ]
}
```

### 15.5 Agent 怎么用它

在 SKILL.md 和 chat.py system prompt 里写了硬规则：**用户提到具体股票/标的时**，agent 必须**先**调 `get_stock_snapshot`，再调 `get_persona` / `search`。回答开头会自动生成一段「市场速览」（≤5 行，列基本面 + 大盘），然后再用第一人称分析。

### 15.6 调试 / 验证

```bash
# CLI 直接调
conda activate bigv-twins && cd /path/to/BigV-twins
python -c "
from bigv_twins.stock_data import get_stock_snapshot, format_snapshot_human
print(format_snapshot_human(get_stock_snapshot('600519')))
print(format_snapshot_human(get_stock_snapshot('宁德时代')))
print(format_snapshot_human(get_stock_snapshot('688981')))
"
```

通过 web 聊天验证：随便问任一博主"你怎么看 X 股票"，回答开头应该出现「在回答之前我先看了下数据：」+ 真实基本面。

### 15.7 已知限制 / TODO

- 暂没拉**股息率 / ROE / 营收同比 / 净利同比**（akshare 财报接口在东方财富一侧抽风，需要找替代源）
- 美股没正经个股数据，agent 只能按 ticker 名透传 + 训练知识
- 没拉**最近重要公告/新闻**（v2 可加）

注：宏观 / 板块 / 资产类话题（港股 / 黄金 / 煤炭 / AI 等）由独立的「主题市场上下文」机制处理 —— 见 §16。

---

## 16. 主题市场上下文（topics.json）

`get_stock_snapshot` 解决"用户提到具体股票"的场景。但很多时候用户问的是**宏观或板块**：「最近港股有什么投资建议」、「黄金现在能买吗」、「煤炭板块还有机会吗」。这些问题里没有具体 ticker，但 agent 仍然需要知道**当前的市场环境**才能给出有依据的回答。

### 16.1 双层召回设计

```
用户消息
   │
   ├── L1: 服务端关键词预扫描（web/chat.py 自动跑）
   │     按 topics.json 里的关键词词典匹配 → 命中就预拉数据
   │     结果以「市场环境」段拼到 system prompt 末尾
   │     这层对 agent 透明 —— 它直接看到数据，不用调工具
   │
   └── L2: agent 主动调用 MCP 工具
         get_market_context(topics=["hk","gold"])
         CLI / Telegram 入口走这条；想补别的主题也可以再调
```

L1 是默认路径（web 用户感觉不到），L2 是兜底 + 主动召回。

### 16.2 主题词典 `topics.json`

项目根目录的 `topics.json` 定义两件事：

**`topics`**：每个 topic id 映射到 keyword 列表 + 该主题要拉哪些 asset

```json
"hk": {
  "label": "港股",
  "keywords": ["港股", "恒生", "港交所", "h 股", "H 股", "港 A"],
  "assets": ["HSI", "HSCEI", "HSTECH"]
}
```

**`assets`**：每个 asset id 映射到取数方式 + Tencent 兜底

```json
"HSI": {
  "name": "恒生指数",
  "type": "ak_hk_index",
  "primary": "HSI",
  "backup_tencent": "hkHSI"
}
```

### 16.3 内置主题（v1 覆盖）

| 类别 | topics |
|---|---|
| 大盘指数 | `a-share` / `gem` / `star` / `bse` / `hk` / `us` |
| 资产 | `gold` |
| 行业 ETF | `industry-bank` · `industry-baijiu` · `industry-coal` · `industry-lithium` · `industry-semi` · `industry-ai` · `industry-new-energy` · `industry-military` · `industry-consumer` · `industry-real-estate` · `industry-resources` |

**热修改**：直接编辑 `topics.json`，1 小时内自动 reload（或 `systemctl --user restart bigv-twins-web` 立即生效）。

### 16.4 数据源（每个 asset 有 primary + Tencent 兜底）

| type | 主源 | 给什么 | Tencent 兜底 |
|---|---|---|---|
| `ak_a_index` | akshare 新浪 `stock_zh_index_daily` | 1 月日线 K 线 → 1 周/1 月走势 | spot only |
| `ak_hk_index` | akshare 东方财富 `stock_hk_index_daily_em` | 1 月日线 | spot only |
| `ak_us_index` | akshare 新浪 `index_us_stock_sina` | 1 月日线 | — |
| `tencent_quote` | Tencent `qt.gtimg.cn/q=` | real-time spot + 当日涨跌 | — |
| `tencent_hf` | Tencent `qt.gtimg.cn/q=hf_*`（现货商品） | spot + 当日涨跌 | — |

每个 asset 独立 try/except，单源失败不影响其他。10 分钟缓存。

### 16.5 输出长这样

agent 看到的 system prompt 末尾自动追加：

```
## 市场环境（系统已自动采集，回答时如用得上请自然引用）

### 港股
- **恒生指数** 现价 25640.08 · 近1周 今日 25640.08, 较昨日 -0.61%
- **国企指数** 现价 8607.55 · 近1周 今日 8607.55, 较昨日 -0.38%
- **恒生科技指数** 现价 4841.72 · 近1周 今日 4841.72, 较昨日 -0.32%
```

### 16.6 添加新 topic

```bash
# 1. 编辑 topics.json
#    - "topics" 加新 topic id + keywords + assets 引用
#    - "assets" 加对应 asset 定义（type/primary/backup_tencent）

# 2. 立即生效（或等 1 小时自动 reload）
ssh ... 'systemctl --user restart bigv-twins-web'
```

举例：加一个「白银」主题

```json
"silver": {
  "label": "白银",
  "keywords": ["白银", "银价"],
  "assets": ["spot_silver"]
},
// 在 assets 里：
"spot_silver": {
  "name": "现货白银",
  "type": "tencent_hf",
  "primary": "hf_SI"
}
```

### 16.7 调试

```bash
# 直接试 detect_topics + get_market_context
python -c "
from bigv_twins.market_data import detect_topics, get_market_context, format_market_context_for_prompt
topics = detect_topics('港股最近怎么样')
print('detected:', topics)
print(format_market_context_for_prompt(get_market_context(topics)))
"
```

### 16.8 已知限制

- HK 指数 primary（akshare 东方财富 `stock_hk_index_daily_em`）经常 503，**已自动 fallback 到 Tencent**（只给 spot，无历史）。如果需要 HK 历史，可以扩展加第二条 fallback（雪球之类）
- 行业 ETF 用的是 `tencent_quote`，**只有 spot 价**，没有 1 周/1 月历史。够用但不深；要历史的话可以改 type 为 `ak_a_share` 走新浪的 `stock_zh_a_daily`（部分 ETF 会失败，需测试）
- topics.json 的关键词是简单 substring 匹配，不做分词，所以 "黄金时代" 也会触发 `gold`。粒度够用，需要更细可以引入 jieba

---

## 17. 多 Agent 架构（bigv 博主分身 + advisor 投顾对照组）

### 17.1 为什么不让 main agent 来扮演博主

OpenClaw `main` agent 自带一个固定 IDENTITY（`小索 / 🧑‍🎓 apprentice/disciple`），
SOUL 里也有「Be the assistant you'd actually want to talk to at 2am」之类的口头禅。
这些注入会**污染**博主角色扮演——agent 偶尔会把自我介绍的语气、emoji、
markdown 偏好混进博主语气，造成身份漂移（「鳄鱼认为」「博主曾说」之类的第三人称叙述）。

解决：把博主分身彻底搬到一个**专门的、无固定身份的** agent 里。

### 17.2 两个 agent + 两个 MCP server

```
                    ┌──────────────────────────────────────┐
                    │  /chat (FastAPI) —— 赛博大V web UI    │
                    │  根据 blogger.agent 选 OpenClaw agent │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
        kind=blogger│                             │ kind=advisor
                    ▼                             ▼
        ┌────────────────────────┐      ┌──────────────────────────┐
        │   openclaw/bigv        │      │   openclaw/advisor       │
        │ (5 位博主角色扮演)      │      │ (中立 AI 投顾，对照组)    │
        └─────┬───────────────┬──┘      └─────┬────────────────┬───┘
              │ MCP           │ MCP           │ MCP            │ Skill
              │               │               │                │
              ▼               ▼               ▼                ▼
   ┌──────────────────┐  ┌──────────────────────────┐  ┌──────────────────┐
   │  bigv-blogger    │  │       bigv-market        │  │  agent-browser   │
   │  :8770 (MCP)     │  │       :8771 (MCP)        │  │  (OpenClaw skill)│
   │ search/persona   │  │ get_stock_snapshot       │  │ web search       │
   │ get_recent/post  │  │ get_market_context       │  │ headless browser │
   │ list_bloggers    │  │ ── 同时给 bigv + advisor │  │                  │
   └──────────────────┘  └──────────────────────────┘  └──────────────────┘
   (仅 bigv 可用)         (bigv + advisor 都可用)        (仅 advisor 可用)
```

要点：
- `bigv` 同时连 `bigv-blogger`（语料）+ `bigv-market`（行情）——两个 MCP 都用
- `advisor` 连 `bigv-market`（行情）+ `agent-browser`（web 搜索）——**禁止**碰 `bigv-blogger`
- `bigv-market` 是**共享**的，两个 agent 都可调（行情数据本来就跟博主无关）

| 维度        | `bigv` agent                              | `advisor` agent                          |
| ----------- | ----------------------------------------- | ---------------------------------------- |
| 用途        | 扮演 5 位归档博主的角色                   | 通用投资顾问，对照组                     |
| Workspace   | `~/.openclaw/workspace-bigv`              | `~/.openclaw/workspace-advisor`          |
| 默认模型    | `bailian/qwen3.5-plus`                    | `bailian/qwen3.5-plus`                   |
| MCP 白名单  | `bigv-blogger.*` + `bigv-market.*`        | `bigv-market.*` only                     |
| Skills      | （不需要，纯靠 MCP）                       | `agent-browser`（用于 web 搜索）         |
| 语料库      | 5 位博主的 RAG                            | **无**（只看公开数据）                    |
| 第一/三人称 | 第一人称（「我认为」）                    | 第三人称（「该股票」「市场」）            |
| 路由        | `bloggers.json` 里 `agent: "bigv"` 时触发 | `bloggers.json` 里 `agent: "advisor"`    |

### 17.3 MCP server 拆分原因

原本一个 `bigv-twins` MCP server 同时暴露**博主语料**（search/persona/...）+
**市场数据**（stock_snapshot/market_context）。advisor agent 不该看到博主语料，
所以按职责拆成两个 server，prompt 层面再给 advisor 加上 `bigv-blogger.*` 黑名单：

- `bigv-twins-blogger.service`（port 8770）—— 博主专用
  - `list_bloggers / search / get_persona / get_recent / get_post`
  - 资源：`persona://blogger/{slug}`
- `bigv-twins-market.service`（port 8771）—— 通用行情
  - `get_stock_snapshot / get_market_context`

OpenClaw 当前**不能**在 config 层级按 agent 过滤 MCP 工具，所以白/黑名单是
通过 IDENTITY/AGENTS.md + system prompt 共同实现的（见 `openclaw/agents/`）。

### 17.4 部署一个新机器（多 agent 部分）

```bash
# 假设 deploy.sh 已跑完（systemd / mcp / web 都起来了）
cd ~/projects/BigV-twins

# 一键 provision 两个 agent
bash openclaw/install_agents.sh
# 它会：
#   1) openclaw agents add bigv     --workspace ~/.openclaw/workspace-bigv
#   2) openclaw agents add advisor  --workspace ~/.openclaw/workspace-advisor
#   3) 把 openclaw/agents/{bigv,advisor}/{IDENTITY,SOUL,AGENTS}.md 复制进对应 workspace
#   4) 把 main workspace 的 agent-browser skill 复制到 workspace-advisor
#   5) 注册 bigv-blogger / bigv-market 两个 MCP server

# 验证
PATH=$HOME/.nvm/versions/node/$(ls ~/.nvm/versions/node | tail -1)/bin:$PATH \
  openclaw agents list
PATH=...   openclaw mcp servers list
```

### 17.5 「AI 投顾」是什么 / 不是什么

**它是**：

- 通用 AI 投资分析助手，**不**模仿任何博主
- 中立、第三方视角；用 K 线 / 均线 / MACD / RSI / 布林带 / 量价等通用框架
- 输出结构：基本面 → 技术面 → 资金面 → 风险点
- 数据来自 `bigv-market` MCP（实时行情 / 估值 / 大盘）+ `agent-browser`（公开新闻）

**它不是**：

- 不是博主分身——它**严格禁止**调用 `bigv-blogger.*` 工具
- 不下买/卖断言（用「关注 / 留意 / 警惕」之类的措辞）
- 不带博主的口头禅、签名、风格

UI 上它在 `/chat` 卡片网格的**最后一位**，紫蓝渐变背景 + 🤖 emoji，
有 `对照组` 标签明确区分。同一个问题用户既能问博主、又能问投顾，
做"两种视角对比"。

### 17.6 修改 agent 身份的流程

1. 改 `openclaw/agents/<agent>/{IDENTITY,SOUL,AGENTS}.md`
2. 跑 `bash openclaw/install_agents.sh`（幂等，会覆盖 workspace 里的版本）
3. 不需要重启 openclaw-gateway——下一次 `/v1/chat/completions` 调用就生效
4. 验证：`curl https://8-155-174-112.nip.io/chat/eyu` 然后随便发个消息，
   看回答风格是否符合预期

如果你在测试时 hand-edit 了 `~/.openclaw/workspace-bigv/AGENTS.md` 直接
试效果，**记得**最后把改动 copy 回 `openclaw/agents/bigv/AGENTS.md` 并提交，
不然下次跑 `install_agents.sh` 会被覆盖。

---

## 18. MCP Server 构建 / 部署速查（commit 拆分总览）

> 这一节是给"想看整个 MCP 是怎么搭起来的"人的快速索引。

### 18.1 代码

| 文件                                                | 职责                                                   |
| --------------------------------------------------- | ------------------------------------------------------ |
| `src/bigv_twins/blogger_server.py`                  | 博主语料 MCP server（FastMCP，streamable-http，:8770） |
| `src/bigv_twins/market_server.py`                   | 市场数据 MCP server（FastMCP，streamable-http，:8771） |
| `src/bigv_twins/search.py`                          | sqlite-vec 检索后端（被 blogger_server 调用）          |
| `src/bigv_twins/stock_data.py`                      | 股票快照（多源；被 market_server 调用）                |
| `src/bigv_twins/market_data.py`                     | 主题市场上下文（被 market_server + web/chat.py 调用）  |
| `pyproject.toml` 里的 `bigv-twins-{blogger,market}-server` entry points | `python -m` 等价的 CLI 入口         |

### 18.2 systemd

| 单元                              | 作用                                          |
| --------------------------------- | --------------------------------------------- |
| `bigv-twins-blogger.service`      | 把 blogger_server 拉常驻在 :8770              |
| `bigv-twins-market.service`       | 把 market_server 拉常驻在 :8771               |

`systemd/install_systemd.sh` 会把它们 enable 到 user-level systemd
（需 `loginctl enable-linger dtl`）。

### 18.3 OpenClaw 注册

```bash
openclaw mcp servers set bigv-blogger --url http://127.0.0.1:8770/mcp --transport streamable-http
openclaw mcp servers set bigv-market  --url http://127.0.0.1:8771/mcp --transport streamable-http
```

由 `openclaw/install_agents.sh` 帮你跑。

### 18.4 详细介绍

- 博主 MCP 工具用法 → §15（股票快照） + §16（市场上下文）
- agent 怎么调用这些工具 → §17.2 表格 + 各 agent 的 `AGENTS.md`
- skills 怎么和 MCP 配合 → §10（skills/ 目录）+ skill 文件里的 `## 工具`

### 18.5 不要做的

- ❌ **不要**让 main agent 调 `bigv-blogger.*`（main 自带的 IDENTITY 会污染博主语气）
- ❌ **不要**让 advisor agent 调 `bigv-blogger.*`（这是策略性禁止，让 advisor 保持中立）
- ❌ **不要**把端口暴露到公网（127.0.0.1 only；走 OpenClaw gateway 才接外部）

---

## License

私有项目，自用。BGE-base-zh-v1.5 (MIT) by BAAI；OpenClaw (商业)；FastMCP (MIT)；FastAPI (MIT)；Pico CSS (MIT)；akshare (MIT)。
