# BigV-twins

> 投资博主的"数字分身" —— 把知乎归档变成可对话的 RAG 知识库，挂在 OpenClaw agent 上随时问。

每个博主 = 一个 [Skill](https://docs.openclaw.ai/clawhub/skill-format.md) + 一个 [persona 摘要](personas/) + 一份 [向量化的归档](twins/) + 一个共享的 MCP server。Agent 收到问题 → 触发对应博主的 skill → 调 MCP server 读 persona 与检索语料 → 用 OpenClaw 配的 LLM 生成带引文的回答。

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
│   ├── config.py                   (4 个博主的 slug↔author_id 映射 + Settings)
│   ├── chunk.py                    (HTML 剥离 + 段落滑窗切分)
│   ├── embed.py                    (sentence-transformers + BGE 封装)
│   ├── index.py                    (增量索引器；CLI: python -m bigv_twins.index)
│   ├── search.py                   (sqlite-vec 检索；CLI: python -m bigv_twins.search)
│   └── server.py                   (FastMCP server；CLI: python -m bigv_twins.server)
│
├── scripts/
│   ├── generate_personas.py        ← 一次性 persona 生成（读 openclaw.json 自动调配的 provider）
│   ├── generate_skills.py          ← 模板化生成 4 个 SKILL.md
│   └── test_mcp_client.py          ← MCP 服务端到端冒烟测试
│
├── systemd/                        ← systemd 单元的源文件 + 安装脚本
│   ├── bigv-twins-server.service   (MCP server 常驻)
│   ├── bigv-twins-daily.service    (每日增量任务)
│   ├── bigv-twins-daily.timer      (定时器：每天 03:17 + 抖动)
│   └── install_systemd.sh          (复制到 ~/.config/systemd/user/ 并 enable)
│
├── skills/                         ← 「真相」：4 个 OpenClaw Skill 的源
│   ├── README.md
│   ├── bigv-mr-dang/SKILL.md
│   ├── bigv-eyu/SKILL.md
│   ├── bigv-sanren/SKILL.md
│   └── bigv-shen/SKILL.md
│
├── personas/                       ← 生成出来的风格指南（小，进仓库）
│   ├── mr-dang.md
│   ├── eyu.md
│   ├── sanren.md
│   └── shen.md
│
├── twins/                          ← 向量化好的 RAG 数据库（每博主一个 .db）
│   ├── mr-dang.db        (4.2 MB)
│   ├── eyu.db            (5.1 MB)
│   ├── sanren.db         (35 MB)
│   └── shen.db           (~300 MB 全量)
│   ⚠ 默认 gitignore；迁移时可单独 rsync 节省重建时间
│
└── logs/                           ← 索引器 + MCP server 日志（gitignored）
    ├── bootstrap.log
    ├── mcp_server.log
    └── daily_index.log
```

### 部署后的「副本」（不在项目里）

| 路径 | 内容 | 关系 |
|---|---|---|
| `~/.config/systemd/user/bigv-twins-*.{service,timer}` | systemd 单元 | 由 `systemd/install_systemd.sh` 复制 |
| `~/.openclaw/workspace/skills/bigv-*/` | 4 个 SKILL.md | 由 `deploy.sh` 从 `skills/` 复制 |
| `~/.openclaw/openclaw.json` 里的 `mcp.servers.bigv-twins` | MCP 连接配置 | 由 `openclaw mcp set` 写入 |
| `~/.cache/huggingface/...` | BGE-base-zh-v1.5 模型权重（~400 MB） | 首次 embed 时自动下载 |

更新 `skills/` 后**必须**重新复制到 OpenClaw workspace（见 §9）。

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

假设你想加 `xinwen-libao` (`example-token-99`)：

```bash
# 1. zhihu 项目那边先把这个博主加进爬虫并跑一遍（不在本项目范围）

# 2. 把博主元信息加到 src/bigv_twins/config.py 的 BLOGGERS tuple:
#    Blogger(slug="xinwen", author_id=5, url_token="example-token-99", name="新闻立波"),

# 3. 加触发别名到 scripts/generate_skills.py 的 ALIASES dict:
#    "xinwen": "立波、新闻立波、libao",

# 4. 重生成 skill + 索引 + persona + 复制
conda activate bigv-twins && cd /path/to/BigV-twins
python scripts/generate_skills.py --blogger xinwen
python -m bigv_twins.index --blogger xinwen
python scripts/generate_personas.py --blogger xinwen
cp -r skills/bigv-xinwen ~/.openclaw/workspace/skills/

# 5. 验证
openclaw skills list | grep bigv-xinwen
openclaw agent --agent main -m "新闻立波 怎么看 X"
```

MCP server **不用重启**——它每次请求都重新打开对应 .db 文件。

---

## 10. 刷新 persona

Persona 会随博主风格 drift 而变得不准。建议每 1–2 月或大事件后刷新一次：

```bash
conda activate bigv-twins && cd /path/to/BigV-twins
python scripts/generate_personas.py --force        # 刷新全部
python scripts/generate_personas.py --blogger eyu --force  # 只刷新一个
```

不需要重启或重新装 skill —— skill 在运行时调 `bigv-twins.get_persona`，永远读最新文件。

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

## License

私有项目，自用。BGE-base-zh-v1.5 (MIT) by BAAI；OpenClaw (商业)；FastMCP (MIT)。
