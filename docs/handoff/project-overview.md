# BigV-twins 项目全景（移交文档 1/3）

**最后更新**: 2026-06-10 v0.7  
**用途**: 给新 agent / 新环境读的完整项目上下文，读完这 3 份文档 + 看代码就能恢复工作状态。

---

## 一句话定位

「投资博主数字分身」— 把知乎上 5 位投资博主的内容做成 RAG persona + 4 位投资大师的经典著作 + AI 投顾对照组 + 每日博主观点汇总 + 投资决策记录 + 成长复盘系统。

## 核心价值

用户（DTL）是一位个人投资者，关注 A 股 / 港股 / 美股。他每天跟踪 5 位知乎投资博主的内容，同时学习巴菲特等投资大师的经典。这个系统帮他：
1. **随时对话** — 问任何博主/大师"你怎么看 XX？"，基于他们真实语料回答
2. **每日汇总** — 不用逐个看博主帖子，一份 Digest 3 分钟掌握全局
3. **记录操作** — 每笔买卖都记录，附思路和计划
4. **AI 复盘** — 每周自动回顾持仓，每月复盘清仓标的，提取投资规则
5. **成长追踪** — 月度/季度成长报告，看自己哪里进步哪里退步

## 项目关系

```
zhihu 归档站（只读，不要改）        BigV-twins（主项目）
/home/dtl/projects/zhihu             /home/dtl/projects/BigV-twins
├── data/zhihu.db (源数据)    ──→    ├── twins/*.db (向量化语料)
├── FastAPI on :8000                 ├── FastAPI on :8001
└── 每天 03:01 爬新帖子              └── 03:21 embedding → 03:30 brief → 03:40 digest
```

- zhihu 归档站是**数据源**，BigV-twins 读取它的 zhihu.db 但不写入
- BigV-twins 的归档链接指向 zhihu 归档站（`:8000/content/{id}`）
- 两个项目共享同一台服务器（8.155.174.112），通过 systemd user service 管理

## GitHub

- BigV-twins: `dtldtt/BigV-twins`（private）
- zhihu: `dtldtt/zhihu`（private）

## 技术栈

- Python 3.12 + FastAPI + SQLite (aiosqlite) + Jinja2 + Pico CSS
- 向量检索: BAAI/bge-base-zh-v1.5 + 自建向量 DB（twins/*.db）
- LLM 对话: OpenClaw gateway → Qwen3.6-flash（月付，无 per-call 成本）
- LLM 总结/回顾: Qoder SDK → ultimate/auto（月付）
- MCP Server: bigv-blogger（语料检索）+ bigv-market（行情数据）
- 部署: miniconda3/envs/bigv-twins, systemd user service, Caddy reverse proxy

## 博主和大师

### 知乎博主（source=zhihu, agent=bigv）
| slug | 名字 | author_id | 特点 |
|------|------|-----------|------|
| mr-dang | MR Dang | 1 | 宏观视角，幽默调侃 |
| eyu | 寒武纪的鳄鱼 | 2 | 已退出，不再更新 |
| sanren | 水又三人禾 | 3 | 技术分析，趋势跟踪 |
| shen | 阳光下的沈同学 | 4 | 宏观配置，红利偏好 |
| paipi | 派大星皮皮 | 5 | 短线操作，量价分析 |

### 投资大师（source=letters/books, agent=bigv）
| slug | 名字 | 语料来源 |
|------|------|---------|
| buffett | 沃伦·巴菲特 | 致股东信 1977-至今 + 股东大会 Q&A |
| munger | 查理·芒格 | 演讲 + 穷查理宝典 |
| graham | 本杰明·格雷厄姆 | 聪明的投资者 + 证券分析 |
| lynch | 彼得·林奇 | 成功投资 + 战胜华尔街 + 理财 |

### AI 投顾（kind=advisor, agent=advisor）
- slug: advisor, 独立第三方分析师，不用博主语料，用市场数据 + web search

## 每天自动跑的定时任务

| 时间 | 任务 | 用什么 |
|------|------|--------|
| 03:01 | zhihu 爬虫拉新帖 | zhihu 项目的 cron |
| 03:21 | BigV-twins embedding 增量索引 | systemd timer |
| 03:30 | 每个博主生成 daily brief | Qoder ultimate |
| 03:40 | 全局 Daily Digest | Qoder ultimate |
| 03:45 | 从 Digest 提取可验证预测 | 代码（无 LLM） |
| 每 2h | 刷新金十新闻 | 代码（无 LLM） |
| 08:00 | ticker_brief 早盘版 | OpenClaw/Qwen |
| 16:00 | 每日行情快照 | 代码（无 LLM） |
| 19:00 | ticker_brief 收盘版 | OpenClaw/Qwen |
| 周六 20:00 | 持仓 AI 回顾（per-ticker） | Qoder ultimate |
| 每月 1 号 06:00 | Persona 月度更新 | Qoder ultimate |
| 每月 1 号 07:00 | 已清仓标的复盘 | Qoder auto |
| 每月 1 号 09:00 | 月度成长复盘 | Qoder performance |
| 每月 1 号 09:30 | 季度成长复盘（1/4/7/10月） | Qoder performance |
| 17:30 | 分红自动同步 | akshare（无 LLM） |

## 版本历史

| 版本 | 日期 | 主要内容 |
|------|------|---------|
| v0.1-v0.4 | 2025 | 基础搭建：MCP server、persona、向量化、Web UI |
| v0.5 | 2026-05 | 决策日志、个股研究页、搜索 |
| v0.6 | 2026-05-28 | 投资记录重设计、成长复盘、分红工具、Token 监控 |
| **v0.7** | **2026-06-10** | **Digest、prompt 全面升级、清仓复盘、趋势追踪、架构重构** |
