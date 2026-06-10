# BigV-twins 技术架构详解（移交文档 2/3）

**最后更新**: 2026-06-10 v0.7

---

## 目录结构

```
BigV-twins/
├── bloggers.json              — 博主/大师配置（单一真相来源）
├── personas/                  — 每个博主/大师的风格画像（*.md）
│   ├── buffett.md / munger.md / graham.md / lynch.md
│   ├── mr-dang.md / eyu.md / sanren.md / shen.md / paipi.md
│   └── （每月 1 号 06:00 自动更新知乎博主的 persona）
├── prompts/                   — 所有 LLM prompt 模板
│   ├── chat/
│   │   ├── advisor.md         — AI 投顾对话
│   │   ├── blogger.md         — 知乎博主分身对话
│   │   ├── master.md          — 大师对话
│   │   └── master-challenge.md — 大师检验模式
│   ├── brief/
│   │   ├── blogger-daily.md   — 每日博主独立总结
│   │   └── daily-digest.md    — 每日全局 Digest
│   ├── review/
│   │   ├── ticker-review.md   — 持仓 per-ticker AI 回顾
│   │   ├── closed-review.md   — 已清仓标的复盘
│   │   ├── monthly-review.md  — 月度成长复盘
│   │   └── single-trade-review.md — 单笔交易回顾（旧版）
│   └── persona/
│       └── update-persona.md  — Persona 月度更新专用
├── twins/                     — 向量化的博主/大师语料（*.db）
├── docs/                      — 文档/测试报告/方案
├── logs/                      — 运行日志
├── src/bigv_twins/
│   ├── config.py              — 配置（Blogger dataclass + Settings）
│   ├── prompt_loader.py       — 读 prompts/*.md + {{变量}} 替换
│   ├── stock_data.py          — akshare 行情/分红数据
│   ├── market_data.py         — 宏观市场上下文
│   ├── embed.py               — BAAI/bge 向量化
│   └── web/
│       ├── app.py             — FastAPI app + APScheduler 定时任务
│       ├── chat.py            — 对话路由（所有对话入口）
│       ├── report.py          — 投资日报 /report
│       ├── journal.py         — 投资记录 /journal
│       ├── stock.py           — 个股研究页 /stock/{ticker}
│       ├── growth.py          — 成长复盘 /growth
│       ├── admin.py           — 管理 /admin
│       ├── digest.py          — Daily Digest 生成
│       ├── blogger_brief.py   — 每日博主独立 brief 生成
│       ├── review_engine.py   — 持仓 AI 回顾引擎
│       ├── closed_review.py   — 已清仓复盘
│       ├── reflection_engine.py — 月度/季度成长复盘
│       ├── persona_updater.py — Persona 月度更新
│       ├── trends.py          — 趋势追踪 /report/trends
│       ├── qoder_call.py      — Qoder SDK 统一封装
│       ├── openclaw_client.py — OpenClaw 对话客户端
│       ├── dividend_sync.py   — A 股分红自动同步
│       ├── news_scraper.py    — 金十新闻抓取
│       ├── db.py              — ORM 模型 + DB 初始化
│       └── templates/         — Jinja2 模板
└── chats.db                   — SQLite 主数据库（gitignored）
```

## 数据库（chats.db）

SQLite 数据库，所有数据都在这一个文件里。核心表：

### 用户和认证
| 表 | 用途 |
|---|------|
| users | 用户账户（含 bcrypt 密码哈希） |
| invites | 邀请码 |

### 对话
| 表 | 用途 |
|---|------|
| conversations | 对话记录（user_id + blogger_slug + mode） |
| messages | 对话消息（role=user/assistant, 含 token_usage） |
| multi_conversations | 多人对话（问所有博主） |

### 投资记录
| 表 | 用途 | 关键字段 |
|---|------|---------|
| decision_journal | 操作记录 | action(open/add/reduce/close/dividend), ticker, price, shares, reasoning, self_critique, record_date |
| investment_notes | 投资随笔 | user_id, content, created_at |
| user_watchlist | 自选股 | user_id, ticker, name |

### AI 生成内容
| 表 | 用途 | 生成时间 |
|---|------|---------|
| blogger_daily_brief | 每个博主每天的独立总结 | 03:30, brief_json 含 7 字段 |
| daily_digest | 全局 Digest | 03:40 |
| ticker_daily_brief | 自选股每日动态 | 08:00 + 19:00 |
| decision_review | AI 回顾（per-ticker + closed） | 周六 20:00 + 每月 1 号 07:00 |
| growth_reports | 月度/季度成长复盘 | 每月 1 号 09:00 |

### 数据积累
| 表 | 用途 | 生成时间 |
|---|------|---------|
| ticker_opinion_log | 博主对个股的情绪标记 | brief 生成时自动写入 |
| prediction_log | 从 Digest 提取的可验证预测 | 03:45 |
| market_snapshot_daily | 关键标的每日收盘快照 | 16:00 |
| backtest_entries | 博主推荐的回测验证 | 定期跑 |

### 监控
| 表 | 用途 |
|---|------|
| token_usage_hourly | Qwen/OpenClaw token 用量（小时粒度） |
| qoder_usage_log | Qoder SDK token 用量（逐次调用） |
| cached_news | 金十新闻缓存 |

### 数据库生成方式

chats.db 由 `db.py` 的 `init_db()` 在应用启动时自动创建（`Base.metadata.create_all`）。新增列通过手动 ALTER TABLE 迁移（在代码里或一次性脚本）。数据库文件在 `.gitignore` 里，不进 git。

## 对话链路

```
用户在 Web UI 发消息
  → chat.py ask() 路由
  → system_prompt_for(blogger, mode) 
  → prompt_loader.load_prompt("chat/master.md", blogger_slug=..., blogger_name=...)
  → 拼 messages = [system, ...history, user]
  → openclaw_client.stream_chat(messages, model=f"openclaw/{blogger.agent}")
  → OpenClaw gateway (localhost:18789) → Qwen3.6-flash
  → SSE 流式返回
```

对话走 OpenClaw/Qwen（月付无 per-call 成本）。总结/回顾走 Qoder SDK（也是月付）。

## LLM 调用分工

| 场景 | 走什么 | 模型 |
|------|--------|------|
| 对话（博主/大师/投顾/检验） | OpenClaw → Qwen3.6-flash | 月付 |
| 博主日报 brief | Qoder SDK | ultimate |
| Daily Digest | Qoder SDK | ultimate |
| 持仓 AI 回顾 | Qoder SDK | ultimate |
| 已清仓复盘 | Qoder SDK | auto |
| 月度成长复盘 | Qoder SDK | performance |
| Persona 更新 | Qoder SDK | ultimate |
| ticker_brief 新闻摘要 | OpenClaw → Qwen | 月付 |

所有 Qoder 调用走 `qoder_call.py`，自动记录 token 用量到 `qoder_usage_log` 表。

## 环境

- 服务器：阿里云 8.155.174.112（国内，GFW 屏蔽部分外网）
- SSH alias: `private`
- Python: `/home/dtl/miniconda3/envs/bigv-twins/bin/python`
- Web service: `systemctl --user restart bigv-twins-web.service`
- 日志: `/home/dtl/projects/BigV-twins/logs/web.log`
- HTTPS: Caddy reverse proxy → `https://8-155-174-112.nip.io`
