"""SQLAlchemy 2.0 async models + engine for the web chat app.

Single sqlite file at `settings.chats_db_path` (project_root/chats.db).
Tables: users, invites, blogger_overrides, conversations, messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, select,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bigv_twins.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'admin' | 'user'
    invite_id: Mapped[int | None] = mapped_column(ForeignKey("invites.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_capital: Mapped[float | None] = mapped_column(Float, nullable=True)  # legacy 万元，迁移后由 cny_principal 替代（保留 backward compat）
    # v0.7: 按币种隔离的资金 — 单位都是 元（人民币：CNY，港币：HKD）
    cny_principal: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # A 股账户本金 (转入-转出净额)
    cny_dividend: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # A 股累计分红入账
    hkd_principal: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # 港股账户本金
    hkd_dividend: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # 港股累计分红入账

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BloggerOverride(Base):
    __tablename__ = "blogger_overrides"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hidden_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False, index=True
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_usage_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ============================================================================
# Multi-blogger conversations ("问所有人" 多人横向对比模式)
#
# Completely independent from single-blogger Conversation/Message above.
# Deletion cascades within multi_* tables; does NOT touch individual chats.
# ============================================================================


class MultiConversation(Base):
    __tablename__ = "multi_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    # JSON-encoded list of blogger slugs participating in this multi-conv.
    # e.g. '["mr-dang","eyu","buffett","advisor"]'
    participant_slugs: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False, index=True
    )

    messages: Mapped[list["MultiMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="MultiMessage.created_at",
    )


class MultiMessage(Base):
    """A single 'turn' marker — either the user's question or the rollup summary.

    Per-blogger answers live in `MultiSubResponse` and FK back to the role='user'
    message that triggered them.
    """
    __tablename__ = "multi_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("multi_conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'summary'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    conversation: Mapped[MultiConversation] = relationship(back_populates="messages")
    sub_responses: Mapped[list["MultiSubResponse"]] = relationship(
        back_populates="user_message",
        cascade="all, delete-orphan",
        order_by="MultiSubResponse.created_at",
    )


class MultiSubResponse(Base):
    """One blogger's answer to one user-message in a multi-conversation."""
    __tablename__ = "multi_sub_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_message_id: Mapped[int] = mapped_column(
        ForeignKey("multi_messages.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 'running' (mid-stream, written as 'done' once SSE completes), 'done', 'error'
    status: Mapped[str] = mapped_column(String(16), default="done", nullable=False)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    user_message: Mapped[MultiMessage] = relationship(back_populates="sub_responses")


# ============================================================================
# Investment report (投资日报) — per-user watchlist + cached daily artifacts
# ============================================================================


class UserWatchlist(Base):
    """A stock in a user's watchlist. Resolved canonical ticker + name.

    Max 30 per user (enforced in router). UNIQUE(user_id, ticker) prevents dupes.
    Ordering: insertion order via `sort_order` (defaulted to id at insert time).
    """
    __tablename__ = "user_watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    # Display name (resolved at add-time, may go stale on rename — refresh on view)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 'a-share' / 'hk' / 'us' — same as TickerInfo.market
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="a-share")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    # 'manual' 用户主动加 / 'auto' 系统因为交易加 — 清仓时只删 'auto'
    added_via: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    __table_args__ = (
        UniqueConstraint("user_id", "ticker", name="uq_watchlist_user_ticker"),
    )


class CachedNews(Base):
    """金十数据「重要事件」缓存。每条带 LLM 判断的利好/利空/中性 verdict。

    Shared across all users (public news). Refreshed every 30 min by APScheduler
    job. Dedup by jin10_id.
    """
    __tablename__ = "cached_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jin10_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    jin10_time: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(String(300), nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)  # 利好 | 利空 | 中性
    verdict_reason: Mapped[str] = mapped_column(String(120), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class BloggerDailyBrief(Base):
    """每日由 advisor agent 总结的博主前一日观点。

    Generated by APScheduler at 03:30 (after zhihu daily timer 03:01 + BigV-twins
    daily indexer 03:21). Dedup by (blogger_slug, brief_date).
    """
    __tablename__ = "blogger_daily_brief"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    brief_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD'
    brief_md: Mapped[str] = mapped_column(Text, nullable=False)
    # 完整结构化 JSON（main_view, key_quotes, ticker_opinions 等 7 字段原样存储）
    brief_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mentioned_tickers: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("blogger_slug", "brief_date", name="uq_brief_slug_date"),
    )


class TickerDailyBrief(Base):
    """每只自选股每日的「相关动态」摘要。

    Shared across all users — the data (news + blogger mentions) is public.
    Generated at 03:35 by APScheduler job that walks all users' watchlist
    unique tickers. UNIQUE(ticker, brief_date).

    Composed of two parts:
      - blogger_mentions: which blogger briefs from blogger_daily_brief
        mentioned this ticker today (free, from JSON of mentioned_tickers field)
      - news_summary_md: LLM-summarized 1-2 sentence(s) of today's news for
        this ticker (via web_search) + verdict 利好/利空/中性
    """
    __tablename__ = "ticker_daily_brief"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    brief_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # JSON list of blogger slugs that mentioned this ticker today
    blogger_mentions: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # LLM-generated 1-2 sentence summary of today's news; empty if no news
    news_summary_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    verdict: Mapped[str] = mapped_column(String(8), default="中性", nullable=False)  # 利好 | 利空 | 中性
    verdict_reason: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("ticker", "brief_date", name="uq_ticker_brief_date"),
    )


class BacktestEntry(Base):
    """每对 (blogger, brief_date, ticker, window_days) 的回测结果。

    blogger 在某天的日报提到某 ticker 之后 N 天，该 ticker vs 沪深300 的超额收益。
    每日 cron 计算新的；窗口未到期的 (exit_price NULL) 第二天接着算。
    """
    __tablename__ = "backtest_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    brief_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    window_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    # Actual prices/dates as fetched (next trading day if entry/exit is non-trading)
    entry_date_actual: Mapped[str | None] = mapped_column(String(10), nullable=True)
    exit_date_actual: Mapped[str | None] = mapped_column(String(10), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(nullable=True)
    exit_price: Mapped[float | None] = mapped_column(nullable=True)
    benchmark_entry: Mapped[float | None] = mapped_column(nullable=True)
    benchmark_exit: Mapped[float | None] = mapped_column(nullable=True)
    ticker_return: Mapped[float | None] = mapped_column(nullable=True)      # %
    benchmark_return: Mapped[float | None] = mapped_column(nullable=True)   # %
    excess_return: Mapped[float | None] = mapped_column(nullable=True)      # %
    hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)         # True=excess>0
    # 'complete' | 'pending' (窗口未到期) | 'no_data' (akshare 拉不到)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # 博主在 brief 里提到这只票的态度（从 ticker_opinion_log 同步过来）
    # 'bullish' (看多) / 'bearish' (看空) / 'avoid' (回避) / 'neutral' (中性) / 'unknown' (没提取到)
    sentiment: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("blogger_slug", "brief_date", "ticker", "window_days",
                         name="uq_bt_slug_date_ticker_window"),
    )


_engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.chats_db_path}",
    echo=False,
    future=True,
)

_SessionFactory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)




class DecisionJournal(Base):
    """投资决策日志 — 记录用户的买入/卖出决策 + 环境快照。"""
    __tablename__ = "decision_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ticker_name: Mapped[str] = mapped_column(String(60), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # buy/sell/add/reduce
    action_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_at_decision: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # User-written decision logic
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    hold_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_signals: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_hold_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    if_drop_10pct: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Auto-collected snapshots (JSON)
    stock_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    blogger_opinions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Status
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, onupdate=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Review scheduling
    next_review_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 用户事后自评（多次追加，自动按 "[M月D日 追加评价] ..." 拼接）
    self_critique: Mapped[str | None] = mapped_column(Text, nullable=True)
    record_date: Mapped[str | None] = mapped_column(String(10), nullable=True)

    user: Mapped[User] = relationship()





class TickerOpinionLog(Base):
    """博主对个股的每日观点记录（自动从 brief 提取）。"""
    __tablename__ = "ticker_opinion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ticker_name: Mapped[str] = mapped_column(String(60), nullable=False)
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    opinion_date: Mapped[str] = mapped_column(String(10), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), default="unspecified", nullable=False)
    is_pivot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_brief_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_at_opinion: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("ticker", "blogger_slug", "opinion_date", name="uq_opinion_ticker_blogger_date"),
    )



class DecisionReview(Base):
    """决策回顾记录 — v0.7 起改 per-ticker：覆盖该股 ticker 的所有操作。

    journal_id 兼容老版（可空）；新版用 ticker 关联到一只股票而非单笔操作。
    """
    __tablename__ = "decision_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journal_id: Mapped[int | None] = mapped_column(ForeignKey("decision_journal.id"), nullable=True, index=True)  # legacy
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # v0.7 新主键
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    review_type: Mapped[str] = mapped_column(String(20), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_signals_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    blogger_opinions_since: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_report_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_reflection: Mapped[str | None] = mapped_column(Text, nullable=True)
    lesson_learned: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

class InvestmentNote(Base):
    """投资随笔 — 用户自由记录投资心得，不绑定具体交易。"""
    __tablename__ = "investment_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    user: Mapped[User] = relationship()


class GrowthReport(Base):
    """投资成长复盘 — 跨时段把交易 + 自评 + 随笔 + 单笔回顾综合做总结。

    period_type: 'month' / 'quarter' / 'manual'
    stats_json: 客观数据快照（盈亏、胜率、持仓天数等）
    key_lessons_json: 提炼出的可执行规则数组（积累成个人知识库）
    """
    __tablename__ = "growth_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    report_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_lessons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    user: Mapped[User] = relationship()



class TokenUsageHourly(Base):
    """每小时 token 用量汇总（从 OpenClaw session JSONL 扫描得到）."""
    __tablename__ = "token_usage_hourly"
    hour: Mapped[str] = mapped_column(String(13), primary_key=True)  # YYYY-MM-DDTHH
    total_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_input: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cache_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cache_create: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    by_agent_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    by_model_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now, nullable=False)

class DailyDigest(Base):
    """每日全局 Digest — 汇总所有博主当天观点的跨博主分析。"""
    __tablename__ = "daily_digest"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_date: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    digest_md: Mapped[str] = mapped_column(Text, nullable=False)
    digest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_type: Mapped[str] = mapped_column(String(4), default="C", nullable=False)
    model: Mapped[str] = mapped_column(String(32), default="ultimate", nullable=False)
    blogger_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class PredictionLog(Base):
    """从 digest 观察清单提取的可验证预测。"""
    __tablename__ = "prediction_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    blogger_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    blogger_name: Mapped[str] = mapped_column(String(60), nullable=False)
    prediction_text: Mapped[str] = mapped_column(Text, nullable=False)
    prediction_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verify_by_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    actual_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    analysis_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketSnapshotDaily(Base):
    """每日关键标的收盘行情快照。"""
    __tablename__ = "market_snapshot_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    ticker_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("snapshot_date", "ticker", name="uq_snapshot_date_ticker"),
    )


class QoderUsageLog(Base):
    """Qoder SDK 每次调用的 token 用量记录。"""
    __tablename__ = "qoder_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    task_detail: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    num_turns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


async def log_qoder_usage(task_type: str, task_detail: str, result_msg,
                          model: str = "unknown") -> None:
    """从 Qoder SDK 的 ResultMessage 提取 usage 写入 DB。"""
    usage = getattr(result_msg, "usage", {}) or {}
    # model 优先用调用方传入的，回退到 model_usage 的 key
    if model == "unknown":
        mu = getattr(result_msg, "model_usage", {}) or {}
        if mu:
            model = next(iter(mu.keys()), "unknown")
    async with _SessionFactory() as s:
        s.add(QoderUsageLog(
            task_type=task_type,
            task_detail=task_detail,
            model=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            num_turns=getattr(result_msg, "num_turns", 0) or 0,
            duration_ms=getattr(result_msg, "duration_ms", 0) or 0,
            total_cost_usd=getattr(result_msg, "total_cost_usd", 0) or 0,
        ))
        await s.commit()


async def init_db() -> None:
    """Create tables if missing. Idempotent; called at app startup.

    Also retrofits UNIQUE indexes onto three tables whose constraints were
    historically defined via post-class `__table_args__` assignment — a
    SQLAlchemy idiom that silently doesn't actually register the constraint,
    so `create_all` produced tables without them. New deployments now get the
    constraints inline; existing DBs need this one-shot retrofit.
    """
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        for stmt in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_user_ticker "
            "ON user_watchlist (user_id, ticker)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_brief_slug_date "
            "ON blogger_daily_brief (blogger_slug, brief_date)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_ticker_brief_date "
            "ON ticker_daily_brief (ticker, brief_date)",
        ):
            await conn.exec_driver_sql(stmt)
        # v0.6: user_watchlist.added_via — 区分手动加 vs 交易自动加
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE user_watchlist ADD COLUMN added_via VARCHAR(16) "
                "NOT NULL DEFAULT 'manual'"
            )
        except Exception:
            pass  # 已经加过
        # v0.6: decision_journal.self_critique — 用户事后自评
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE decision_journal ADD COLUMN self_critique TEXT"
            )
        except Exception:
            pass
        # v0.6: backtest_entries.sentiment — 推荐/看空/中性分类
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE backtest_entries ADD COLUMN sentiment VARCHAR(16) "
                "NOT NULL DEFAULT 'unknown'"
            )
        except Exception:
            pass
        # v0.7: 按币种隔离的资金 — A 股/港股账户彻底分开
        for col in ("cny_principal", "cny_dividend", "hkd_principal", "hkd_dividend"):
            try:
                await conn.exec_driver_sql(
                    f"ALTER TABLE users ADD COLUMN {col} FLOAT NOT NULL DEFAULT 0"
                )
            except Exception:
                pass
        # v0.7: decision_review.ticker — per-ticker reviews（替代 per-journal）
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE decision_review ADD COLUMN ticker VARCHAR(16)"
            )
        except Exception:
            pass
        # 一次性把老 total_capital (万元) 迁到 cny_principal (元) — 仅当 cny_principal 还是 0 时
        try:
            await conn.exec_driver_sql(
                "UPDATE users SET cny_principal = COALESCE(total_capital, 0) * 10000 "
                "WHERE cny_principal = 0 AND total_capital IS NOT NULL"
            )
        except Exception:
            pass
        # FTS5 全站搜索表（trigram tokenizer 支持中文）。Contentless 模式 — 我们
        # 自己管理 INSERT/DELETE，不挂触发器（少耦合）
        await conn.exec_driver_sql("""
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                source, title, body,
                ref_id UNINDEXED, ref_url UNINDEXED, ref_date UNINDEXED,
                tokenize='trigram'
            )
        """)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yields a session, commits on success, rolls back on error."""
    async with _SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def find_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def find_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()
