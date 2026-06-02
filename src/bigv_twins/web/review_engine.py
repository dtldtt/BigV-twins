"""决策回顾引擎 — 自动定期回顾用户的投资决策。

APScheduler 每日 20:00 扫描 decision_journal WHERE status='active' AND next_review_at <= today，
自动生成回顾报告。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select, update, func

from bigv_twins.config import settings

from . import db
from .db import DecisionJournal, DecisionReview, TickerOpinionLog
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.review_engine")

# 中文动作标签 — 给模型的 prompt 用，比 'open'/'add' 这种英文 enum 更准确
# A 股常见行业 → 行业 ETF Tencent symbol（用于算"同期同行业涨跌"）
# 不在表里的行业自动跳过，不影响主流程
_INDUSTRY_TO_ETF = {
    "有色金属": "sh512400", "黄金": "sh518880",
    "证券": "sh512000", "保险": "sh512070", "银行": "sh512800",
    "白酒": "sh512690", "食品饮料": "sh512690",
    "医药": "sh512010", "医药生物": "sh512010", "中药": "sh159647",
    "汽车": "sh515030", "新能源车": "sh515030", "汽车整车": "sh515030",
    "汽车零部件": "sh515030",
    "军工": "sh512710", "国防军工": "sh512710",
    "煤炭": "sh515220", "采掘": "sh515220",
    "电力": "sz159611", "公用事业": "sz159611",
    "通信": "sh515880", "电子": "sh515260", "半导体": "sh512760",
    "传媒": "sh512980", "钢铁": "sh515210",
    "化工": "sz159870", "石油石化": "sh159930",
    "建材": "sz159929", "建筑装饰": "sh516950", "建筑材料": "sz159929",
    "机械设备": "sh516960",
    "家用电器": "sz159996",
    "食品": "sz159928",
    "纺织服装": "", "轻工制造": "",  # 没合适 ETF
}


_ACTION_ZH = {
    "open": "建仓（首次买入）",
    "add": "加仓",
    "reduce": "减仓",
    "close": "清仓",
    "retroactive": "补录（已持有的旧仓位）",
}


class _FakeW:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


_REVIEW_PROMPT = """你是一个投资回顾助手。下面是一笔交易的所有相关数据，请生成事后回顾。

## 原始决策
- 标的：{ticker_name}（{ticker}）
- 操作：{action_zh}
- 决策日：{decision_date}
- 决策价：¥{decision_price}
- 决策理由：{reasoning_section}
{plan_section}{fundamentals_then_section}
## 当前状态
- 当前价：¥{current_price}
- 距决策涨跌：{pnl_pct}
- 持有天数：{days_passed} 天
{fundamentals_now_section}{benchmark_section}{opinions_section}{self_critique_section}
## 输出要求

用 Markdown 输出 4 段，总长 300 字以内：

1. **表现回顾**：客观数据复述 — 涨跌幅、跑赢/跑输沪深300多少、估值（PE/PB）变化、博主情绪变化。一段话讲完。

2. **逻辑验证**：{verify_instruction}

3. **结合你自己的反思**：{self_critique_instruction}

4. **下一步建议**：基于上面所有客观数据，给出一个具体可执行的建议（继续持有 / 加仓 / 减仓 / 清仓），并说明理由。不要含糊地说"密切关注"。

【硬约束】
- 不要编造任何不在上面数据里的信息（PE / 市值 / 行业新闻 / 财报数字 / 同行对比都禁止瞎说）
- {reasoning_constraint}
"""


def _format_fundamentals_then(journal: DecisionJournal) -> str:
    """从 journal.stock_snapshot 拼决策时基本面。没快照就返回 ''。"""
    if not journal.stock_snapshot:
        return ""
    try:
        snap = json.loads(journal.stock_snapshot)
    except (json.JSONDecodeError, TypeError):
        return ""
    bits = []
    if snap.get("pe") is not None:
        bits.append(f"PE {snap['pe']:.1f}")
    if snap.get("pb") is not None:
        bits.append(f"PB {snap['pb']:.2f}")
    if snap.get("market_cap") is not None:
        bits.append(f"市值 {snap['market_cap']:.0f} 亿")
    if not bits:
        return ""
    return f"- 决策时基本面：{' / '.join(bits)}\n"


def _format_fundamentals_now(quote: dict) -> str:
    bits = []
    if quote.get("pe") is not None:
        bits.append(f"PE {quote['pe']:.1f}")
    if quote.get("pb") is not None:
        bits.append(f"PB {quote['pb']:.2f}")
    if quote.get("market_cap") is not None:
        bits.append(f"市值 {quote['market_cap']:.0f} 亿")
    if not bits:
        return ""
    return f"- 当前基本面：{' / '.join(bits)}\n"


def _fetch_csi300_return(start_date_str: str) -> float | None:
    """拉 start_date 到今天的沪深 300 涨跌幅 %。失败返回 None。"""
    try:
        from .backtest import _fetch_benchmark_hist, _get_close_on_or_after
        from datetime import datetime
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        today_dt = date.today()
        df = _fetch_benchmark_hist(
            start_dt.strftime("%Y%m%d"),
            (today_dt + timedelta(days=1)).strftime("%Y%m%d"),
        )
        if df is None or len(df) == 0:
            return None
        start_close = _get_close_on_or_after(df, start_date_str)
        end_close = _get_close_on_or_after(df, today_dt.strftime("%Y-%m-%d"))
        if not start_close or not end_close:
            return None
        return (end_close[1] / start_close[1] - 1.0) * 100.0
    except Exception as e:
        log.warning("csi300 return fetch failed: %s", e)
        return None


async def generate_review_for_journal(journal: DecisionJournal) -> str | None:
    """Generate one review report for a journal entry. Returns markdown or None."""
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(journal.ticker)])
    quote = quotes[0] if quotes else {}
    current_price = quote.get("current")

    if not current_price or not journal.price_at_decision:
        return None

    pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100
    days_passed = (date.today() - journal.created_at.date()).days if journal.created_at else 0

    # 基本面对比段
    fundamentals_then_section = _format_fundamentals_then(journal)
    fundamentals_now_section = _format_fundamentals_now(quote)

    # 沪深300 同期对比段
    benchmark_section = ""
    if journal.created_at:
        decision_date_str = journal.created_at.strftime("%Y-%m-%d")
        csi_ret = await loop.run_in_executor(None, _fetch_csi300_return, decision_date_str)
        if csi_ret is not None:
            excess = pnl_pct - csi_ret
            benchmark_section = (
                f"- 同期沪深300涨跌：{csi_ret:+.1f}%，本仓位超额 {excess:+.1f}%\n"
            )

    # 决策后博主观点
    opinions_section = ""
    async with db._SessionFactory() as session:
        opinion_rows = await session.execute(
            select(TickerOpinionLog).where(
                TickerOpinionLog.ticker == journal.ticker,
                TickerOpinionLog.opinion_date > (journal.created_at.strftime("%Y-%m-%d") if journal.created_at else ""),
            ).order_by(TickerOpinionLog.opinion_date.desc()).limit(5)
        )
        opinions = list(opinion_rows.scalars())
        if opinions:
            opinions_section = "\n## 决策后的博主观点\n"
            for op in opinions:
                opinions_section += f"- {op.opinion_date} [{op.blogger_slug}] {op.sentiment}: {op.summary}\n"

    plan_section = ""
    if journal.action_detail:
        plan_section = f"- 操作计划：{journal.action_detail}\n"
    if journal.target_price:
        plan_section += f"- 目标价：¥{journal.target_price}\n"
    if journal.stop_loss_price:
        plan_section += f"- 止损价：¥{journal.stop_loss_price}\n"

    # 用户自评（self_critique）
    if journal.self_critique and journal.self_critique.strip():
        self_critique_section = f"\n## 用户事后自评（按时间累积）\n{journal.self_critique}\n"
        self_critique_instruction = (
            "用户已经写了上述自评。把它跟你看到的客观数据对照：哪些观察一致？"
            "哪些用户没注意到但数据能体现？给出一段综合性的反思（不是简单重复用户原话）。"
        )
    else:
        self_critique_section = ""
        self_critique_instruction = (
            "用户还没写过自评。基于客观数据指出一个最值得用户事后写一笔自评的点（"
            "比如：当初仓位是不是太重、卖飞了某个加仓机会、或者反过来没及时止损）。"
        )

    # reasoning 空时的硬约束 —— 用户明确要求：不要推测买入逻辑
    if journal.reasoning and journal.reasoning.strip():
        reasoning_section = journal.reasoning[:300]
        verify_instruction = "结合上面的决策理由，看当初的判断现在站得住吗？引用具体数字。"
        reasoning_constraint = "决策理由是用户自己写的，可以基于它做验证"
    else:
        reasoning_section = "（用户没有记录当时的思路）"
        verify_instruction = (
            "用户当时没记录思路。**严格禁止推测**当初的买入逻辑。"
            "本节请改成纯客观数据点评：涨跌幅、基本面变化、跟沪深300的差距，"
            "陈述事实，不要替用户脑补 \"当时可能是因为 PE 低\" 这类心理活动。"
        )
        reasoning_constraint = (
            "用户没记录理由 — **绝对不要推测**他当初为什么买（会误导他）。"
            "在 \"逻辑验证\" 段只复述客观数据，不要替他构造心理活动"
        )

    prompt = _REVIEW_PROMPT.format(
        ticker_name=journal.ticker_name,
        ticker=journal.ticker,
        action_zh=_ACTION_ZH.get(journal.action, journal.action),
        decision_date=journal.created_at.strftime("%Y-%m-%d") if journal.created_at else "?",
        decision_price=f"{journal.price_at_decision:.2f}",
        reasoning_section=reasoning_section,
        plan_section=plan_section,
        fundamentals_then_section=fundamentals_then_section,
        current_price=f"{current_price:.2f}",
        pnl_pct=f"{pnl_pct:+.1f}%",
        days_passed=days_passed,
        fundamentals_now_section=fundamentals_now_section,
        benchmark_section=benchmark_section,
        opinions_section=opinions_section,
        self_critique_section=self_critique_section,
        verify_instruction=verify_instruction,
        self_critique_instruction=self_critique_instruction,
        reasoning_constraint=reasoning_constraint,
    )

    return await _call_qoder(prompt, journal.id)


async def _call_qoder(prompt: str, journal_id: int) -> str | None:
    """走 Qoder SDK performance 模式（推理重的任务比 flash 强很多，不会乱编 PE）。"""
    if not settings.qoder_personal_access_token:
        log.warning("review %d skipped: QODER_PERSONAL_ACCESS_TOKEN not set", journal_id)
        return None
    try:
        from qoder_agent_sdk import (
            AssistantMessage, QoderAgentOptions, access_token, query,
        )
    except ImportError as e:
        log.warning("qoder_agent_sdk import failed: %s", e)
        return None

    options = QoderAgentOptions(
        auth=access_token(settings.qoder_personal_access_token),
        model="performance",
    )
    pieces: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            pieces.append(c.get("text", ""))
                        elif hasattr(c, "text"):
                            pieces.append(c.text)
                elif isinstance(content, str):
                    pieces.append(content)
    except Exception as e:
        log.warning("qoder review failed for journal %d: %s", journal_id, e)
        return None
    text = "".join(pieces).strip()
    return text or None


# Review interval: 7→30→90→180 days
_REVIEW_INTERVALS = [7, 30, 90, 180]


_TICKER_REVIEW_PROMPT = """【你的身份】
你是一位拥有 15+ 年中国 A 股资本市场实战经验的资深投资顾问，熟悉宏观周期、行业框架、估值体系、交易心理学，特别擅长帮助个人投资者复盘自己的交易行为、识别认知或执行上的偏差，并通过启发式追问引导他们自我成长。

你的回顾不是判官式打分，而是**教练式对话** —— 帮用户看见自己的盲区，推动他们做下一笔更好的决策。

【语气基线】
可以做客观、有依据的**批判性评价**，但不要武断定性人或能力。
- ❌ 不说："你不适合投资大宗商品"、"你不适合做投资"、"这次操作很失败"、"能力不足"
- ✅ 可以说：
  - "这次没完全按你最初写的计划执行"
  - "没关注到 X 这条利空"
  - "补仓节奏看起来比原计划激进"
- 每条批判后必须紧跟一个**具体可执行**的未来改进方向。
- 优势要主动说：用户做对的、坚持得好的，明确点出来。

【自然语言计划解读规则 — 极其重要】
用户写的"操作计划"是自然语言，**往往省略且有歧义**。遇到歧义必须分情况讨论，**严禁武断按一种解读下结论**。

例：用户写"跌10%补一笔"
  → 解读 A：跌 10% **只补一次**（之后不再补）
  → 解读 B：**每跌 10% 补一次**（滚动补仓）
  正确写法：
  "你的计划『跌10%补一笔』有两种合理解读：
   - 如果是 A（只补一次），那么后面那两次加仓是计划之外的
   - 如果是 B（滚动补），那么实际节奏（11%/20%/17%）跟计划基本吻合
   建议你在自评里明确一下当时的真实意图。"

错误写法："你说补一次结果补了三次" — 这不是复盘，是抠字眼。

【对没记录 reasoning + 没自评的操作】
作为资深投顾，你可能看出某些操作从专业角度值得追问（如：建仓后 7 天内大幅加仓 / 卖出后短期买回 / 反复短线 / 单股突然加到很重仓位等）。
- **严禁替用户脑补当时的逻辑**（禁推测硬约束依然在）
- 但**可以温和提醒**："这笔 X 操作从专业视角看有点意外（说明哪里反常），建议你补一笔自评把当时的真实想法记下来 —— 这是发现自己决策模式的关键素材。"

【数据真实性硬约束】
- 不要编造任何不在下面数据里的信息（PE / 市值 / 行业新闻 / 财报数字）
- 引用数字必须出自下面"客观快照"段
- {reasoning_constraint}

# 标的：{ticker_name}（{ticker}）{industry_section}

# 客观快照
{stats_md}

# 决策时基本面（首次建仓时）
{fundamentals_then_section}
# 当前基本面
{fundamentals_now_section}
# 同期对比基准
{benchmark_section}

# 全部操作（按时间顺序）
{operations_md}

# 决策后博主观点
{opinions_section}

# 用户对各操作的事后自评
{critiques_md}

---

# 输出要求

用 Markdown 输出 5 段，总长 800-1300 字。**严格按这个顺序**：

## 1. 持仓全貌
基于客观快照 + 操作列表，一段话讲清这只股票的持仓轨迹。引用具体数字。

## 2. 逻辑验证 + 计划兑现度
{verify_instruction}

**必须显式对比**原始 action_plan（计划）跟实际操作之间的差异：
- 如果用户写过计划，先**判断该计划是否有歧义**（参考上面的"自然语言计划解读规则"），有歧义就分情况讨论，**绝不武断按一种解读下结论**
- 把客观事实摆出来（计划怎么写、实际怎么做、哪里一致哪里不同），不下"对错"判断
- 偏离不一定错（市场变了计划该调整），但要让用户看见事实

## 3. 下一步操作建议
**眼前的具体动作**：继续持有 / 加仓 / 减仓 / 清仓 + 触发条件 + 数字目标。
不要含糊地说"密切关注"或"伺机而动"。
如果该股已清仓，本段改成"复盘要点"：1-2 条从这段持仓能带走的经验。

## 4. 关键风险点（最多 2 条）
基于上面数据指出**当前持仓最值得警惕的 1-2 个风险**。每条必须挂具体数字。
例：「集中度风险：单股占 A 股账户 28%」「估值悬挂风险：PE 122 倍处于近 3 年 95 分位」
不要泛泛说"市场波动"、"政策风险"。

## 5. 成长引导 — 教练式启发（这是最重要的一段）

**5a. 反思追问**（必有）
{self_critique_instruction}
追问要**开放、具体、能引出新认知**，不是给答案。
例：「3/16 那笔加仓，是基于新的基本面判断，还是想摊薄成本？这两种动机指向的下一步操作是完全不同的。」

**5b. 模式提示**（如能从数据看出）
如果用户在多个仓位上有相同模式（你只看到这一只股票的数据，但可以从这一只里 spot 出可能跨股的习惯），点出来供用户自查。
例：「你在这只股票上有『越跌越买、补仓无上限』的模式，建议自查其他持仓是否也有类似特征。」

**5c. 学习方向**（强烈建议给）
基于这只票本次复盘暴露的**具体**不足，推荐 1-2 个**通用方法论或概念**（不要推荐具体书名 — 容易记错或编造）。学习方向必须**跟这次的具体问题挂钩**，不要泛泛推荐"看巴菲特"。
例：
- 补仓节奏没有预设上限 → "可以了解凯利公式 / 固定比例资金管理"
- 估值判断很少引用财务数据 → "可以学习 DCF / EV/EBITDA / 历史 PE 分位法"
- 周期股频繁补仓 → "可以了解商品周期理论（库存周期 / 产能周期）"
"""


# 行业信息缓存（akshare 单股查询慢，存盘）
import json as _json
from pathlib import Path
_INDUSTRY_CACHE_PATH = Path("/tmp/bigv_ticker_industry.json")
_industry_cache: dict[str, str] = {}
_industry_loaded = False


def _fetch_industry_for(ticker: str) -> str | None:
    """从 akshare 拉个股的行业归属。失败/无数据返 None。增量磁盘缓存。"""
    global _industry_loaded
    if not _industry_loaded:
        if _INDUSTRY_CACHE_PATH.exists():
            try:
                _industry_cache.update(_json.loads(_INDUSTRY_CACHE_PATH.read_text()))
            except Exception:
                pass
        _industry_loaded = True
    if ticker in _industry_cache:
        return _industry_cache[ticker] or None
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=ticker)
        # df 是 (item, value) 两列
        ind = None
        for _, row in df.iterrows():
            if row.get("item") == "行业":
                ind = str(row.get("value") or "").strip()
                break
        _industry_cache[ticker] = ind or ""
        try:
            _INDUSTRY_CACHE_PATH.write_text(_json.dumps(_industry_cache, ensure_ascii=False))
        except Exception:
            pass
        return ind or None
    except Exception as e:
        log.warning("industry fetch failed for %s: %s", ticker, e)
        return None


def _fetch_52w_range(ticker: str, today_dt: date) -> tuple[float, float] | None:
    """近 1 年最高/最低收盘价。失败返 None。"""
    try:
        from .backtest import _fetch_price_hist
        end_str = today_dt.strftime("%Y%m%d")
        start_str = (today_dt - timedelta(days=370)).strftime("%Y%m%d")
        df = _fetch_price_hist(ticker, start_str, end_str)
        if df is None or len(df) == 0:
            return None
        # 列名根据 akshare 版本：'收盘'/'最高'/'最低'
        if "最高" in df.columns and "最低" in df.columns:
            high = float(df["最高"].max())
            low = float(df["最低"].min())
        else:
            high = float(df["收盘"].max())
            low = float(df["收盘"].min())
        return (low, high)
    except Exception as e:
        log.warning("52w range fetch failed for %s: %s", ticker, e)
        return None


def _fetch_industry_etf_return(industry: str | None, start_date: date, end_date: date) -> tuple[str, float] | None:
    """根据行业名查 ETF symbol，拉同期涨跌幅 %。返回 (etf_code_no_prefix, return_pct)."""
    if not industry:
        return None
    sym = _INDUSTRY_TO_ETF.get(industry)
    if not sym:
        return None
    try:
        from .daily_brief import _fetch_tencent_batch, _parse_index_tilde
        from .backtest import _fetch_price_hist, _get_close_on_or_after
        # 拉历史价用 akshare（symbol 去掉前缀只留 6 位）
        code = sym[2:]
        df = _fetch_price_hist(code, start_date.strftime("%Y%m%d"),
                               (end_date + timedelta(days=1)).strftime("%Y%m%d"))
        if df is None or len(df) == 0:
            return None
        s_close = _get_close_on_or_after(df, start_date.strftime("%Y-%m-%d"))
        e_close = _get_close_on_or_after(df, end_date.strftime("%Y-%m-%d"))
        if not s_close or not e_close:
            return None
        ret = (e_close[1] / s_close[1] - 1.0) * 100.0
        return (code, ret)
    except Exception as e:
        log.warning("industry ETF return fetch failed (%s/%s): %s", industry, sym, e)
        return None


async def _compute_position_pct(user_id: int, ticker: str, ticker_currency: str,
                                  ticker_market_value: float) -> float | None:
    """该仓位占总资产比例 %。total_assets = principal + dividend + sum(MV) (per currency)。"""
    from .db import User
    async with db._SessionFactory() as s:
        user = await s.get(User, user_id)
        rows = await s.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.status == "active",
                DecisionJournal.action != "dividend",
            )
        )
        all_active = list(rows.scalars())
    if not user:
        return None

    # 拉所有 active ticker 的实时报价（按币种聚合 MV）
    all_tickers = list({j.ticker for j in all_active})
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(t) for t in all_tickers])
    cur_map = {q["ticker"]: (q.get("current"), q.get("currency", "CNY")) for q in quotes if q.get("ok")}

    # 重建持仓股数（按 ticker 累计 buy/reduce/close）
    by_ticker: dict[str, list] = {}
    for j in all_active:
        by_ticker.setdefault(j.ticker, []).append(j)
    total_mv_per_ccy: dict[str, float] = {"CNY": 0.0, "HKD": 0.0}
    for t, ops in by_ticker.items():
        ops.sort(key=lambda x: x.created_at or "")
        shares = 0
        for j in ops:
            n = j.shares or 0
            if j.action in ("open", "add"):
                shares += n
            elif j.action == "retroactive":
                shares = n
            elif j.action == "reduce":
                shares -= n
            elif j.action == "close":
                shares = 0
        if shares <= 0:
            continue
        cur_price, ccy = cur_map.get(t, (None, "CNY"))
        mv = (cur_price or 0) * shares
        total_mv_per_ccy[ccy] = total_mv_per_ccy.get(ccy, 0) + mv

    if ticker_currency == "HKD":
        principal = user.hkd_principal or 0
        total_mv = total_mv_per_ccy.get("HKD", 0)
    else:
        principal = user.cny_principal or 0
        total_mv = total_mv_per_ccy.get("CNY", 0)

    # dividend 用实时 SUM（已到期的）
    today_dt = datetime.combine(date.today(), datetime.max.time())
    div_sum = 0.0
    async with db._SessionFactory() as s:
        rows = await s.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.action == "dividend",
                DecisionJournal.created_at <= today_dt,
            )
        )
        for j in rows.scalars():
            from bigv_twins.stock_data import resolve_ticker as _rt
            info = _rt(j.ticker)
            ccy = info.currency if info else "CNY"
            if ccy == ticker_currency:
                div_sum += (j.price_at_decision or 0) * (j.shares or 0)

    total_assets = principal + div_sum + total_mv
    if total_assets <= 0:
        return None
    return ticker_market_value / total_assets * 100


async def generate_review_for_ticker(user_id: int, ticker: str) -> str | None:
    """生成 per-ticker 综合回顾（覆盖该股全部操作）。

    取当前 cycle 的所有 entries（最近一次 close 之后的；如果从没 close
    过就是全部），整合成一份 Qoder performance 报告。
    """
    async with db._SessionFactory() as session:
        rows = await session.execute(
            select(DecisionJournal).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.ticker == ticker,
            ).order_by(DecisionJournal.created_at)
        )
        all_entries = list(rows.scalars())
    if not all_entries:
        return None

    # 找当前 cycle: 最后一次 close 之后的所有 entries（包括 dividend）
    last_close_idx = -1
    for i, e in enumerate(all_entries):
        if e.action == "close":
            last_close_idx = i
    if last_close_idx >= 0:
        # cycle entries = 最后 close 之前到 close 本身（一个完整 cycle）OR
        # 之后到现在（一个新 cycle）
        # 这里取 close 之后的；如果 close 是最后一条，cycle 就是从开始到 close
        cycle_after = all_entries[last_close_idx + 1:]
        if cycle_after:
            entries = cycle_after  # 重开了新 cycle
            cycle_status = "active"
        else:
            entries = all_entries  # 整个 history 一个 cycle (closed)
            cycle_status = "closed"
    else:
        entries = all_entries
        cycle_status = "active" if all_entries[-1].action != "close" else "closed"

    latest = entries[-1]
    ticker_name = latest.ticker_name

    # 实时报价
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(ticker)])
    quote = quotes[0] if quotes else {}
    current_price = quote.get("current")

    # 客观快照计算
    buys = [e for e in entries if e.action in ("open", "add", "retroactive")]
    sells = [e for e in entries if e.action in ("reduce", "close")]
    divs = [e for e in entries if e.action == "dividend"]
    total_buy_cost = sum((b.price_at_decision or 0) * (b.shares or 0) for b in buys)
    total_buy_shares = sum(b.shares or 0 for b in buys)
    total_sell_proceeds = sum((s.price_at_decision or 0) * (s.shares or 0) for s in sells)
    total_sell_shares = sum(s.shares or 0 for s in sells)
    total_div_amount = sum((d.price_at_decision or 0) * (d.shares or 0) for d in divs)
    cur_shares = total_buy_shares - total_sell_shares
    avg_buy = total_buy_cost / total_buy_shares if total_buy_shares else 0
    # adjusted cost (proceeds + dividends 反哺)
    adj_cost_total = total_buy_cost - total_sell_proceeds - total_div_amount
    adj_cost_per_share = adj_cost_total / cur_shares if cur_shares > 0 else 0
    market_value = (current_price or 0) * cur_shares if cur_shares > 0 else 0
    unrealized = (current_price - adj_cost_per_share) * cur_shares if (current_price and cur_shares > 0) else 0
    earliest_date = entries[0].created_at.date() if entries[0].created_at else date.today()
    days_held = (date.today() - earliest_date).days

    stats_lines = [
        f"- 当前 cycle 状态：{'持仓中' if cycle_status == 'active' else '已清仓'}",
        f"- 持仓周期：{earliest_date} → 今天，共 {days_held} 天",
        f"- 操作数：建仓/加仓 {len(buys)} 次 / 减仓清仓 {len(sells)} 次 / 分红 {len(divs)} 次",
        f"- 累计买入：{total_buy_shares} 股，成本 ¥{total_buy_cost:.2f}（买入均价 ¥{avg_buy:.2f}）",
        f"- 累计卖出：{total_sell_shares} 股，收回 ¥{total_sell_proceeds:.2f}",
        f"- 累计分红：¥{total_div_amount:.2f}",
    ]
    if cur_shares > 0:
        stats_lines.append(f"- 当前持仓：{cur_shares} 股，调整后成本 ¥{adj_cost_per_share:.2f}/股")
        if current_price:
            stats_lines.append(f"- 当前市值：¥{market_value:.0f}（现价 ¥{current_price:.2f}）")
            stats_lines.append(f"- 浮动盈亏：¥{unrealized:+.0f}（{(unrealized/abs(adj_cost_total)*100 if adj_cost_total else 0):+.1f}%，已包含分红反哺）")
        # 仓位占总账户多少
        ticker_currency = quote.get("currency") or "CNY"
        try:
            pct = await _compute_position_pct(user_id, ticker, ticker_currency, market_value)
            if pct is not None:
                ccy_label = "A股账户" if ticker_currency == "CNY" else "港股账户"
                stats_lines.append(f"- 该仓位占{ccy_label}总资产：{pct:.1f}%")
        except Exception as e:
            log.warning("position pct compute failed for %s: %s", ticker, e)
    stats_md = "\n".join(stats_lines)

    # 基本面
    fundamentals_then_section = ""
    first_open = next((e for e in entries if e.action in ("open", "retroactive")), None)
    if first_open and first_open.stock_snapshot:
        try:
            snap = json.loads(first_open.stock_snapshot)
            bits = []
            if snap.get("pe") is not None:
                bits.append(f"PE {snap['pe']:.1f}")
            if snap.get("pb") is not None:
                bits.append(f"PB {snap['pb']:.2f}")
            if snap.get("market_cap") is not None:
                bits.append(f"市值 {snap['market_cap']:.0f} 亿")
            if bits:
                fundamentals_then_section = f"- {' / '.join(bits)}（{first_open.created_at.date() if first_open.created_at else '?'}）\n"
        except (json.JSONDecodeError, TypeError):
            pass

    # 行业（仅 A 股）+ 52 周区间
    industry_section = ""
    industry: str | None = None
    is_a_share = ticker.isdigit() and len(ticker) == 6
    if is_a_share:
        industry = await loop.run_in_executor(None, _fetch_industry_for, ticker)
        if industry:
            industry_section = f"\n# 所属行业\n- {industry}\n"

    fundamentals_now_section = ""
    bits = []
    if quote.get("pe") is not None:
        bits.append(f"PE {quote['pe']:.1f}")
    if quote.get("pb") is not None:
        bits.append(f"PB {quote['pb']:.2f}")
    if quote.get("market_cap") is not None:
        bits.append(f"市值 {quote['market_cap']:.0f} 亿")
    if bits:
        fundamentals_now_section = f"- {' / '.join(bits)}\n"
    # 52 周区间（A 股）
    if is_a_share and current_price:
        rng = await loop.run_in_executor(None, _fetch_52w_range, ticker, date.today())
        if rng:
            low, high = rng
            if high > low:
                pct_in_range = (current_price - low) / (high - low) * 100
                pos_label = "底部" if pct_in_range < 25 else ("中部" if pct_in_range < 75 else "顶部")
                fundamentals_now_section += (
                    f"- 当前 ¥{current_price:.2f} 处于 52 周区间 ¥{low:.2f}-¥{high:.2f}，"
                    f"分位 {pct_in_range:.0f}%（{pos_label}）\n"
                )

    # 沪深300 同期 + 同行业 ETF 同期
    benchmark_section = ""
    csi_ret = None
    try:
        from .backtest import _fetch_benchmark_hist, _get_close_on_or_after
        df = await loop.run_in_executor(
            None, _fetch_benchmark_hist,
            earliest_date.strftime("%Y%m%d"),
            (date.today() + timedelta(days=1)).strftime("%Y%m%d"),
        )
        if df is not None and len(df) > 0:
            start = _get_close_on_or_after(df, earliest_date.strftime("%Y-%m-%d"))
            end = _get_close_on_or_after(df, date.today().strftime("%Y-%m-%d"))
            if start and end:
                csi_ret = (end[1] / start[1] - 1.0) * 100.0
                benchmark_section = f"- 沪深300 同期涨跌：{csi_ret:+.1f}%\n"
    except Exception as e:
        log.warning("csi300 fetch for ticker review failed: %s", e)
    # 同行业 ETF
    if is_a_share and industry:
        try:
            ind_ret_pair = await loop.run_in_executor(
                None, _fetch_industry_etf_return, industry, earliest_date, date.today()
            )
            if ind_ret_pair:
                etf_code, ind_ret = ind_ret_pair
                # 这只票的同期收益
                from .backtest import _fetch_price_hist, _get_close_on_or_after as _gc
                df_t = await loop.run_in_executor(
                    None, _fetch_price_hist, ticker,
                    earliest_date.strftime("%Y%m%d"),
                    (date.today() + timedelta(days=1)).strftime("%Y%m%d"),
                )
                ticker_ret = None
                if df_t is not None and len(df_t) > 0:
                    s = _gc(df_t, earliest_date.strftime("%Y-%m-%d"))
                    e_ = _gc(df_t, date.today().strftime("%Y-%m-%d"))
                    if s and e_:
                        ticker_ret = (e_[1] / s[1] - 1.0) * 100
                line = f"- 同行业 {industry} 指数（{etf_code} ETF）同期：{ind_ret:+.1f}%"
                if ticker_ret is not None:
                    excess = ticker_ret - ind_ret
                    line += f"，本股同期 {ticker_ret:+.1f}%，超额 {excess:+.1f}%"
                benchmark_section += line + "\n"
        except Exception as e:
            log.warning("industry ETF block failed: %s", e)

    # 操作列表
    op_lines = []
    for e in entries:
        d = e.created_at.strftime("%Y-%m-%d") if e.created_at else "?"
        action_zh = _ACTION_ZH.get(e.action, e.action)
        if e.action == "dividend":
            op_lines.append(f"- {d} 💸 派息 ¥{(e.price_at_decision or 0):.3f}/股 × {e.shares or 0} 股 = ¥{(e.price_at_decision or 0) * (e.shares or 0):.2f}")
        else:
            line = f"- {d} {action_zh} ¥{(e.price_at_decision or 0):.2f} × {e.shares or 0} 股"
            if e.reasoning and e.reasoning.strip():
                line += f"\n  理由：{e.reasoning[:200]}"
            if e.action_detail and e.action_detail.strip():
                line += f"\n  计划：{e.action_detail[:150]}"
            op_lines.append(line)
    operations_md = "\n".join(op_lines) if op_lines else "（无操作）"

    # 决策后博主观点
    opinions_section = ""
    cutoff_str = earliest_date.strftime("%Y-%m-%d")
    async with db._SessionFactory() as session:
        opinion_rows = await session.execute(
            select(TickerOpinionLog).where(
                TickerOpinionLog.ticker == ticker,
                TickerOpinionLog.opinion_date >= cutoff_str,
            ).order_by(TickerOpinionLog.opinion_date.desc()).limit(8)
        )
        opinions = list(opinion_rows.scalars())
    if opinions:
        opinions_section = "\n".join(
            f"- {op.opinion_date} [{op.blogger_slug}] {op.sentiment}: {op.summary}"
            for op in opinions
        )
    else:
        opinions_section = "（无博主观点）"

    # 用户自评
    crit_lines = []
    for e in entries:
        if e.self_critique and e.self_critique.strip():
            d = e.created_at.strftime("%Y-%m-%d") if e.created_at else "?"
            action_zh = _ACTION_ZH.get(e.action, e.action)
            crit_lines.append(f"- 关于 {d} {action_zh} 的自评：\n  {e.self_critique[:400]}")
    critiques_md = "\n".join(crit_lines) if crit_lines else "（用户未写过自评）"

    # 验证措辞 + reasoning 约束
    has_any_reasoning = any(e.reasoning and e.reasoning.strip() for e in entries if e.action != "dividend")
    if has_any_reasoning:
        verify_instruction = "对照各次操作的『理由』看：当初的判断到现在站得住吗？引用具体的客观快照数字。"
        reasoning_constraint = "用户记录了部分理由，可以基于它做验证"
    else:
        verify_instruction = (
            "用户没记录买卖理由。**严格禁止推测**当时的买入逻辑。"
            "本段请改成纯客观数据点评：累计盈亏、加减仓节奏、跟沪深300 的差距。"
            "陈述事实，不要替用户脑补当时心理活动。"
        )
        reasoning_constraint = (
            "用户未记录理由 — **绝对不要推测**他当初为什么买（会误导他）。"
            "在『逻辑验证』段只复述客观数据"
        )

    has_critique = any(e.self_critique and e.self_critique.strip() for e in entries)
    if has_critique:
        self_critique_instruction = (
            "用户已经写过上述自评。把它跟客观数据对照：哪些观察一致？"
            "哪些用户没注意到但数据能体现？给一段综合反思（不要简单复述用户原话）。"
        )
    else:
        self_critique_instruction = (
            "用户还没在这只股票上写过自评。基于客观数据指出最值得用户事后写一笔自评的点（"
            "比如：仓位太重、卖飞、未及时止损）。"
        )

    prompt = _TICKER_REVIEW_PROMPT.format(
        ticker=ticker,
        ticker_name=ticker_name,
        industry_section=industry_section,  # 可空字符串
        stats_md=stats_md,
        fundamentals_then_section=fundamentals_then_section or "（无快照数据）",
        fundamentals_now_section=fundamentals_now_section or "（拉取失败）",
        benchmark_section=benchmark_section or "（拉取失败）",
        operations_md=operations_md,
        opinions_section=opinions_section,
        critiques_md=critiques_md,
        verify_instruction=verify_instruction,
        reasoning_constraint=reasoning_constraint,
        self_critique_instruction=self_critique_instruction,
    )

    return await _call_qoder(prompt, hash(ticker))


async def save_ticker_review(user_id: int, ticker: str, report_md: str) -> "DecisionReview":
    """把生成好的 markdown 存到 decision_review 表（per-ticker），返回 ORM 对象。

    注意：SQLite ALTER 不能改 NOT NULL 约束，journal_id 依然是必填，
    所以这里塞最近一次该 ticker 的 active 操作 id（不是真的"绑死"那笔操作，
    只是为了满足 schema），主键关联还是看 ticker 字段。
    """
    loop = asyncio.get_running_loop()
    quotes = await loop.run_in_executor(None, get_watchlist_quotes, [_FakeW(ticker)])
    cur_price = quotes[0].get("current") if quotes else None
    async with db._SessionFactory() as s:
        # 找一个该 ticker 的 journal id 来满足 NOT NULL 约束
        any_j = await s.scalar(
            select(DecisionJournal.id).where(
                DecisionJournal.user_id == user_id,
                DecisionJournal.ticker == ticker,
            ).order_by(DecisionJournal.created_at.desc()).limit(1)
        )
        rows = await s.execute(
            select(DecisionReview).where(
                DecisionReview.user_id == user_id,
                DecisionReview.ticker == ticker,
            ).order_by(DecisionReview.created_at.desc())
        )
        prior = list(rows.scalars())
        review_count = len(prior)
        if review_count == 0: review_type = "1week"
        elif review_count == 1: review_type = "1month"
        elif review_count == 2: review_type = "3month"
        else: review_type = "6month"
        if review_count > 8:
            review_type = "manual"
        review = DecisionReview(
            journal_id=any_j,  # placeholder to satisfy NOT NULL
            ticker=ticker,
            user_id=user_id,
            review_type=review_type,
            current_price=cur_price,
            review_report_md=report_md,
        )
        s.add(review)
        await s.commit()
        await s.refresh(review)
    return review


async def run_scheduled_reviews() -> int:
    """每周六 20:00 cron — 给每个 user 的所有 active ticker 生成一份新的 per-ticker 回顾。

    v0.7+ 起改成固定周节奏：不再 gate next_review_at，每周扫一遍当前
    所有 active 持仓的 (user, ticker)，无论之前回顾过多少次都重新做一份。
    旧报告保留在 decision_review 表里（按时间倒序展示）。
    """
    count = 0

    async with db._SessionFactory() as session:
        rows = await session.execute(
            select(DecisionJournal.user_id, DecisionJournal.ticker)
            .where(
                DecisionJournal.status == "active",
                DecisionJournal.action != "dividend",
            )
            .distinct()
        )
        pairs = [(r[0], r[1]) for r in rows]

    log.info("review engine (weekly): %d (user, ticker) to review", len(pairs))

    for user_id, ticker in pairs:
        try:
            report_md = await generate_review_for_ticker(user_id, ticker)
        except Exception as e:
            log.exception("ticker review failed user=%s ticker=%s: %s", user_id, ticker, e)
            continue
        if not report_md:
            continue
        await save_ticker_review(user_id, ticker, report_md)
        count += 1

    log.info("review engine: generated %d ticker reviews", count)
    return count
