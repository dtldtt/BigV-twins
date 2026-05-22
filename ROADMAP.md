# BigV-twins 改进路线图 (Roadmap)

> 2026-05-22 起。按产品价值 + ROI 排序。
> 完成项目把 `- [ ]` 改成 `- [x]` 并附 commit hash。
> 重大改动同时更新 README 对应章节。

---

## 项目目标（再次锚定）

> 「希望能有不同大师的赛博分身，能站在他们很专业的角度给我一些建议——
> 毕竟只问 AI 的话，回答的东西不一定靠谱。」

核心价值 = **可信源（精挑细选的博主 + 投资大师）+ 多视角对比**。
评估每个 idea 时回到这条线检验。

---

## Tier 1：高 ROI，优先做（目标 1-2 周完成）

### 🌟 #1 加 3-4 位投资大师（互补 Buffett 的盲区）

Buffett 单独的盲区：A 股不熟 / 科技股保留 / 衍生品反感 / 不谈周期。
加几位**互补**大师能补上。每位复用 `scripts/ingest_buffett.py` 框架。

- [ ] **Howard Marks** — 周期 / 风险视角
  - 数据源：[oaktreecapital.com/insights/memos](https://www.oaktreecapital.com/insights/memos)（1990-current, ~150 篇 PDF）
  - 在 highper 上 wget 抓 PDF → marker 转 markdown → `scripts/ingest_marks.py`
  - persona 重点：周期循环、风险三阶段、「等待球进入击球区」
- [ ] **Peter Lynch** — 成长 / 散户视角
  - 数据源：《One Up On Wall Street》《Beating the Street》《Learn to Earn》
  - PDF 需要自己找；Lynch 的演讲文字稿网上有
  - persona 重点：「buy what you know」、PEG 估值、十倍股六种类型
- [ ] **Charlie Munger** — 心智模型 / 跨学科
  - 数据源：《Poor Charlie's Almanack》（已有 PDF）+ 历年 Daily Journal 股东会 transcripts
  - persona 重点：心智模型、Lollapalooza 效应、反向思考、「invert, always invert」
- [ ] **段永平** — 中国本土价值视角
  - 数据源：[xueqiu.com/duanyongping](https://xueqiu.com/duanyongping)（雪球长文 + 帖子）+ Stanford 演讲
  - 这位能补 Buffett 不敢评 A 股的缺口
  - persona 重点：本分、平常心、不为清单、苹果论

**为什么排 #1**：是 #2 多博主对比的"乘数"——10 位视角 vs 5 位视角的差距是质变。
**投入**：每位 1-2h 数据准备 + 半天 persona 调优；总共 2-3 天。

---

### ✅ #2 「问所有人」按钮 — 多博主对比视图 (完成, commit `<本 commit>`)

**痛点**：现在用户问「茅台怎么看」要切 5-10 个对话窗口。

**实现**：
- [x] `/chat` 主页加 ⚡「多人对话」入口
- [x] 后端：fan-out 同一个 prompt 到选定的 agent，并行 SSE（用户可选 2-10 位）
- [x] 拿到所有回答后，用 `openclaw/advisor` 做汇总：对照表格 + 一致/分歧/缺位 + 综合判断
- [x] 前端 SSE 多流复用，按 blogger_slug 路由到对应卡片
- [x] 删除按钮 cascade 限定多人会话子树
- [x] README §22 完整文档

**展示格式参考**：
```
                 茅台 (600519) — 全部视角
┌────────────┬──────────┬──────────────────────────┬──────────────┐
│ 视角        │ 倾向      │ 一句话观点                │ 关键引用      │
├────────────┼──────────┼──────────────────────────┼──────────────┤
│ MR Dang    │ 高股息持有 │ 4.4% 股息 + 消费品长期空间  │ 24-06 文     │
│ 鳄鱼        │ 不在能力圈 │ 民企消费，不是央企/资源     │ 24-01 八条原则│
│ 三人禾      │ 弱右侧     │ 跌破 1300 不见底买点        │ 26-05 周记   │
│ Buffett    │ ❌不评    │ A 股个股不在能力圈           │ -            │
│ AI 投顾    │ 数据中立   │ PE 19.9 历史低位，但需企稳   │ 实时数据     │
└────────────┴──────────┴──────────────────────────┴──────────────┘
一致：估值不贵
分歧：右侧 vs 左侧；买入 vs 观望
缺位：技术派单独的看图判断
```

**为什么排 #2**：这是别人没法 copy 的核心差异化——别的产品没你这套精选语料。
用户每次对比的认知收益**远大于**单独问 N 次。

**投入**：1-2 天（fan-out + 一个总结 LLM 调用 + 前端表格）。

---

### 🌟 #3 投资日志 + 每日早报

**痛点**：当下是 ad-hoc 问答工具，要「想起来才用」。投资是**长期跟踪**——
用户想要的不只是查一次，是每天早晨知道「昨晚到今早发生了什么」。

- [ ] **关注列表 (watchlist)**：用户在 UI 标记关注股票（5-15 只）+ 默认关注所有博主
  - DB 表：`user_watchlist(user_id, slug_or_ticker, kind, added_at)`
  - UI：每个 about 页 / 每个对话框加 ⭐「加入关注」
- [ ] **每日早报生成器**（07:30 systemd timer 触发）
  - 你关注的标的：行情变化 + 重要公告（走 web_search）+ 涉及到的博主新内容
  - 你关注的博主：他们昨日新帖摘要（接 zhihu daily timer 的产物）
  - 大盘环境一句话（复用 market_context）
  - 整合成一份 markdown 早报，存 `chats.db` 的 `daily_brief` 表
- [ ] **首页 widget**：进 `/chat` 时优先显示当日早报
- [ ] **邮件推送**（08:00 trigger）：smtp 走个免费 SMTP（QQ 邮箱 / Gmail / 阿里云 DM）
  - 用户 settings 加「订阅早报」开关 + 邮箱字段
  - 邮件正文 = markdown 早报渲染成 HTML

**为什么排 #3**：把产品从「工具」升级成「日常习惯」——这是留存的关键。
数据全部现成（daily timer 已经在跑了）。

**投入**：3-4 天（watchlist UI + 早报生成 + 邮件发送）。

---

## Tier 2：中期值得做（按价值排）

### A. 博主对某只股票观点的时间演变
- [ ] `/about/<blogger>` 下加 "提到的股票" tab
- [ ] 实现方式：build 一个 ticker-mention 索引（在 daily indexer 里用正则 `\b[036]\d{5}\b|港股 \d{5}` 提取 ticker，写入新表 `chunks_ticker_mentions`）
- [ ] 点茅台 → 该博主所有提及茅台的帖子按时间序，标注每条的情绪倾向
- 看一位博主对某标的观点的演变，对长线持有判断价值很大
- 投入：1 天（建索引 + UI）

### B. `get_financials` MCP 工具
- [ ] 新增 `bigv-market.get_financials(query)`
- [ ] 历史 PE/PB 5 年分位（当前在过去 5 年的百分位）
- [ ] 财务三表 5 年趋势（营收 / 毛利率 / 净利 / 经营现金流 / ROE）
- [ ] 数据源：`akshare.stock_financial_abstract` / `stock_a_lg_indicator`
- 当前财报类问题都靠 web_search 兜底，质量不稳；做成结构化更可靠
- 投入：1 天

### C. 博主信号回看 / 命中率
- [ ] 用 LLM 扫描每位博主历史的「具体看多 / 看空言论」（包含 ticker + 时间 + 倾向）
- [ ] 落表 `blogger_signals(blogger, ticker, signal_date, direction, source_zhihu_id)`
- [ ] 对照标的之后 3/6/12 月涨跌
- [ ] 每位博主一个 "命中率" 仪表板（不要打分太严，给方向感即可）
- 让用户对每位博主信号质量有量化感知；也能反过来改进博主分身的 system prompt
- 投入：4-5 天（信号抽取靠 LLM，要批跑 + 验证）

### D. Cross-blogger 共识 / 分歧仪表盘
- [ ] 首页加「本周热议股票」widget
- [ ] 统计本周所有博主提到的股票频次 + 用 LLM 一次性标 sentiment
- [ ] 输出三栏：「高共识买入 / 高分歧 / 共同回避」
- 这种交叉视图是 alpha 的潜在来源
- 投入：2 天（依赖 A 的 ticker-mention 索引）

### E. 模型多元化（成本 / 速度优化）
- [ ] 简单查询用 Qwen Turbo（更快更便宜）
- [ ] 复杂分析用 Qwen Max / Claude 4.7
- [ ] 路由逻辑根据 prompt 长度 + 关键词决定
- 收益看实际用量；当前自用流量不大，不急

---

## ❌ 不建议做（明确放弃，节制）

- **Portfolio risk analysis**（行业集中度 / 相关性 / VaR）—— 太重，市场有专业工具（Wind / 同花顺等）
- **PWA / 手机 app** —— web mobile 已经够用，做 native 是大坑
- **多用户社交 / 评论** —— 跟"私人投资助手"定位冲突
- **自动交易接口 / 模拟盘** —— 法律 + 风险两边都不该碰
- **聊天机器人推到微信群 / 公众号** —— 合规风险，私人用就好

---

## 已完成（按 commit 时间倒序）

- `fc6f209` 分红双算法（历史口径 + 预测口径）+ prompt 强制展示规则
- `8e1641a` 三层数据架构 Layer 2 — web_search MCP + advisor/bigv 决策树
- `543acd4` 新增 get_dividend_history MCP 工具
- `e48edf5` README §19 大师归档 + §20 Agentic RAG 升级
- `094f5c2` ingest_buffett source_id seq 计数器修正
- `6a80efb` 大师归档框架 + 巴菲特 ingester + UI
- `cc716b5/647e84c` bge-m3 切换 + 距离阈值校准 + README 大更
- `c4585eb` ab_compare.py 检索质量对照
- `ea1e7bf` Agentic RAG 轻量改造（工具描述 + search_multi_query）
- `db29529` 多模型 + 多数据源支持的基础设施
- `3685a65` UI 网格 / advisor 排序 / BOOTSTRAP 自我介绍 / 52 周区间修复
- `7c94d31` 新增 advisor agent（对照组）+ openclaw/ 配置目录
- `6f73e7d` 拆分 MCP 服务 + 新增 bigv agent（博主分身专用）

参见 README §17-§21 当前架构总览。

---

## 维护这份 roadmap

- 每次完成一项：`- [ ]` → `- [x] (commit-hash)`，挪到「已完成」末尾
- 新增 idea：先评估「跟核心目标对齐吗？」「ROI 排哪一档？」再加
- 半年回顾：删掉已完成超过 N 个月的细节，保留 commit 链
