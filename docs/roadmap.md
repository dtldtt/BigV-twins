# 赛博大V 产品路线图

**最后更新**: 2026-06-09

---

## 已完成

### Prompt 审查与升级（2026-06-08）

- [x] P0-1: per-ticker AI 回顾 prompt — 身份强化 + 仓位/行业/基准/风险
- [x] P0-2: 月度成长复盘 prompt — 6 段教练式 + 月环比对照
- [x] P0-3: 博主日报总结 prompt — 7 字段 JSON + 金句/事件/操作/vs 昨日
- [x] P1-4: AI 投顾对话 prompt — 身份强化 + 分析原则 + 股息率判断框架
- [x] P1-5/6/7: 大师/博主分身/检验模式 prompt — 通用化（适配 4 位大师）

### 架构重构（2026-06-08）

- [x] Prompt 模板化 — 7 个 prompt 从 Python 字符串迁移到 `prompts/*.md`
- [x] Qoder SDK 统一封装 — `qoder_call.py`，默认 ultimate 模型

### Daily Digest（2026-06-08 ~ 06-09）

- [x] Prompt 设计与评测 — A/B/C 方案 × 新旧 prompt × auto/performance/ultimate 交叉测试
- [x] 确定方案：新 prompt × 方案 C（原文+brief）× Qoder ultimate
- [x] 完整实现：生成 + 归档（daily_digest 表）+ /report 展示 + 日期选择器 + 手动重新生成
- [x] 定时任务：每天 03:40 自动生成

### Token 用量监控（2026-06-08）

- [x] Qoder SDK 用量采集 — qoder_usage_log 表，按 model/task_type 分类
- [x] /admin/cost 独立页面 — Qoder 折线图 + 模型切换 + Qwen 图表 + 月度归档

### Bug 修复与 UI 改进（2026-06-08 ~ 06-09）

- [x] 博主日报 markdown 格式 + 原文归档链接
- [x] 分红自动计算持股数（按股权登记日）+ record_date 字段
- [x] Journal 当日涨跌显示 + 红涨绿跌
- [x] zhihu DB immutable=1 导致读不到新数据
- [x] 博主观点卡片折叠 + 原文链接新标签页打开

### 趋势追踪 Phase 1（2026-06-09）

- [x] prediction_log 表 — 从 digest 观察清单自动提取可验证预测
- [x] market_snapshot_daily 表 — 每日关键标的收盘行情快照
- [x] /report/trends 时间线页面 — 大 V 观点 × 用户操作 × 用户随笔

---

## 近期计划（1-4 周）

### 趋势追踪 Phase 2：自动验证

- [ ] 每周一 08:00 定时任务：拉上周的实际行情（akshare）
- [ ] 自动对比 prediction_log 的预测方向 vs 实际走势
- [ ] 更新 verdict 字段（correct / wrong / inconclusive）
- [ ] /report/trends 展示验证结果

### 趋势追踪 Phase 3：周度复盘报告

- [ ] 每周一用 LLM（Qoder ultimate）生成周度复盘
- [ ] 输入：本周 5 天的 digest + prediction_log 验证结果 + 用户随笔
- [ ] 输出：做对了什么 / 做错了什么及应对 / 值得记住的规则
- [ ] 新建 weekly_review 表归档
- [ ] prompt 设计：`prompts/review/weekly-trends.md`

### 趋势追踪 Phase 4：投资规则知识库

- [ ] 从周度复盘中提取可复用的投资规则
- [ ] prediction_rule 表：规则文本 + 来源博主 + 验证次数 + 命中率
- [ ] /report/trends 页面展示已验证规则列表

### 趋势追踪 Phase 5：博主能力画像

- [ ] 每月用 LLM 生成/更新博主画像
- [ ] 擅长领域 / 风格 / 最可靠场景 / 容易错的地方
- [ ] /report/trends 页面展示

---

## 中期计划（1-3 个月）

### Prompt 审查（待完成）

- [ ] P1-5/6/7 prompt 详细 review + 对比测试
- [ ] P2-8/9/10: 情绪回填 + jin10/ticker_brief 利好利空评估

### 分红系统完善

- [ ] 工作日自动重跑 dividend_sync（修复周末 akshare 不可用问题）
- [ ] 历史分红数据回填

### 架构优化

- [ ] 评估 OpenClaw skill 与 Python prompt 的统一方案
- [ ] 考虑 Qwen 替代 Qoder 的成本对比（利用 qoder_usage_log 数据）

---

## 长期愿景

- 从"信息聚合"进化到"决策辅助系统"
- 用户的投资规则知识库持续增长，最终形成个人投资系统
- 大 V 的分析方法论被提取、验证、内化为用户自己的工具
- 大师经典（Buffett/Munger/Graham/Lynch）+ 实战大 V + 用户自己的经验 = 三层知识体系
