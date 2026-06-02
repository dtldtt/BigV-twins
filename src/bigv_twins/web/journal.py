"""投资决策日志 v3 — 完整持仓计算 + 自动加自选。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BY_SLUG

from . import auth, db
from .db import BloggerDailyBrief, DecisionJournal, DecisionReview, InvestmentNote, TickerOpinionLog, User, UserWatchlist
from bigv_twins.stock_data import resolve_ticker
from .daily_brief import get_watchlist_quotes

log = logging.getLogger("bigv_twins.web.journal")
router = APIRouter(prefix="/journal")

PKG_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))

def _fromjson_filter(s):
    if not s:
        return {}
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

templates.env.filters['fromjson'] = _fromjson_filter


class _FakeW:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = ticker
        self.market = "A"
        self.note = ""
        self.id = 0


def _collect_stock_snapshot(ticker: str) -> dict:
    quotes = get_watchlist_quotes([_FakeW(ticker)])
    q = quotes[0] if quotes else {}
    return {
        "price": q.get("current"),
        "change_pct": q.get("change_pct"),
        "pe": q.get("pe"),
        "pb": q.get("pb"),
        "market_cap": q.get("market_cap"),
    }


async def _collect_blogger_opinions(ticker: str, ticker_name: str = "") -> list[dict]:
    """Match by ticker code OR name (mentioned_tickers stores codes, but legacy
    journal entries may have Chinese name as the ticker field)."""
    fourteen_days_ago = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    # Build the set of values that should trigger a match
    match_set = {ticker}
    if ticker_name:
        match_set.add(ticker_name)
    # Also resolve ticker → code if it's a Chinese name
    try:
        info = resolve_ticker(ticker)
        if info:
            match_set.add(info.code)
            match_set.add(info.name)
    except Exception:
        pass

    async with db._SessionFactory() as session:
        rows = await session.execute(
            select(BloggerDailyBrief).where(
                BloggerDailyBrief.brief_date >= fourteen_days_ago
            ).order_by(BloggerDailyBrief.brief_date.desc())
        )
        opinions = []
        for br in rows.scalars():
            try:
                mentioned = json.loads(br.mentioned_tickers or "[]")
            except json.JSONDecodeError:
                continue
            if any(m in mentioned for m in match_set):
                blogger = BY_SLUG.get(br.blogger_slug)
                opinions.append({
                    "slug": br.blogger_slug,
                    "name": blogger.name if blogger else br.blogger_slug,
                    "date": br.brief_date,
                    "excerpt": (br.brief_md or "")[:150],
                })
        return opinions[:5]


async def _fill_snapshot(journal_id: int):
    await asyncio.sleep(0.5)
    async with db._SessionFactory() as session:
        journal = await session.get(DecisionJournal, journal_id)
        if not journal:
            return
        try:
            loop = asyncio.get_running_loop()
            snapshot = await loop.run_in_executor(None, _collect_stock_snapshot, journal.ticker)
            journal.stock_snapshot = json.dumps(snapshot, ensure_ascii=False)
        except Exception as e:
            log.warning("stock snapshot failed for %s: %s", journal.ticker, e)
        try:
            opinions = await _collect_blogger_opinions(journal.ticker, journal.ticker_name or "")
            journal.blogger_opinions = json.dumps(opinions, ensure_ascii=False)
        except Exception as e:
            log.warning("blogger opinions failed for %s: %s", journal.ticker, e)
        # Market snapshot: collect major indices
        try:
            from .daily_brief import get_global_indices
            loop = asyncio.get_running_loop()
            indices = await loop.run_in_executor(None, get_global_indices)
            market_data = {i["name"]: i["current"] for i in indices if i.get("current")}
            journal.market_snapshot = json.dumps(market_data, ensure_ascii=False)
        except Exception as e:
            log.warning("market snapshot failed: %s", e)
        # Master wisdom: search master RAG DBs (sync calls wrapped in executor)
        try:
            from bigv_twins.search import search as rag_search
            loop = asyncio.get_running_loop()
            wisdom = []
            for master_slug in ("buffett", "munger", "graham", "lynch"):
                try:
                    hits = await loop.run_in_executor(
                        None,
                        lambda s=master_slug: rag_search(s, journal.ticker_name, top_k=2),
                    )
                    for h in hits:
                        if h.distance < 1.0:
                            wisdom.append({
                                "master": master_slug,
                                "title": h.column_title,
                                "excerpt": h.text[:150],
                                "url": h.url,
                            })
                except Exception:
                    pass
            if wisdom:
                existing = json.loads(journal.market_snapshot) if journal.market_snapshot else {}
                existing["__master_wisdom"] = wisdom[:4]
                journal.market_snapshot = json.dumps(existing, ensure_ascii=False)
        except Exception as e:
            log.warning("master wisdom search failed: %s", e)

        journal.next_review_at = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        await session.commit()


async def _auto_add_watchlist(user_id: int, ticker: str, ticker_name: str):
    """If ticker not in user's watchlist, add it."""
    async with db._SessionFactory() as session:
        existing = await session.execute(
            select(UserWatchlist).where(
                UserWatchlist.user_id == user_id,
                UserWatchlist.ticker == ticker,
            )
        )
        if existing.scalar_one_or_none():
            return
        market = "A"
        if len(ticker) == 5:
            market = "HK"
        wl = UserWatchlist(
            user_id=user_id, ticker=ticker, name=ticker_name, market=market, note="",
            added_via="auto",
        )
        session.add(wl)
        try:
            await session.commit()
            log.info("auto-added %s to watchlist for user %d", ticker, user_id)
        except IntegrityError:
            await session.rollback()  # duplicate (race condition)
        except Exception as e:
            await session.rollback()
            log.warning("auto-add watchlist failed: %s", e)


def _build_portfolio(journals: list, price_map: dict, quote_map: dict,
                     total_capital: float | None) -> list[dict]:
    """Build portfolio from all journals. Calculates cost basis and avg buy price."""
    # Group journals by ticker, ordered by time
    from collections import defaultdict
    ticker_ops: dict[str, list] = defaultdict(list)
    ticker_name_map: dict[str, str] = {}

    for j in journals:
        if j.status != "active":
            continue
        ticker_ops[j.ticker].append(j)
        ticker_name_map[j.ticker] = j.ticker_name

    portfolio = []
    for ticker, ops in ticker_ops.items():
        ops.sort(key=lambda x: x.created_at or "")

        total_shares = 0
        total_cost = 0.0  # total money spent on buys (for cost basis)
        total_buy_shares = 0  # only buy ops (for avg buy price)
        total_buy_amount = 0.0  # only buy ops

        for j in ops:
            shares = j.shares or 0
            price = j.price_at_decision or 0

            if j.action in ("open", "add"):
                total_shares += shares
                total_cost += shares * price
                total_buy_shares += shares
                total_buy_amount += shares * price
            elif j.action == "retroactive":
                # Retroactive: set position directly
                total_shares = shares
                total_cost = shares * price
                total_buy_shares = shares
                total_buy_amount = shares * price
            elif j.action == "reduce":
                # Reduce: 已实现盈亏锁进剩余成本 — total_cost 减"卖出收入"而不是
                # avg cost × 卖出股数。这样卖高价后剩余仓位的成本会"变低"，
                # 用户更直观看到 "之前涨那一波已经吃到了"。
                # 例：买 100@¥58 + 100@¥60 (cost ¥11800) → 卖 100@¥75 (proceeds ¥7500)
                #     剩余 100 股，cost = 11800 − 7500 = ¥4300，成本 ¥43/股
                if total_shares > 0 and shares > 0:
                    sold_shares = min(shares, total_shares)
                    total_shares -= sold_shares
                    total_cost -= sold_shares * price  # subtract PROCEEDS not avg cost
            elif j.action == "close":
                # 一个完整 cycle 结束 — 重置全部状态（包括 buy_shares，因为下次
                # 重新建仓时 "买入均价" 应该只看新 cycle）
                total_shares = 0
                total_cost = 0
                total_buy_shares = 0
                total_buy_amount = 0
            elif j.action == "dividend":
                # 现金分红：股数不变，cost 减总分红金额（实现盈利）
                # price_at_decision = 每股派息（元）；shares = 当时持仓股数
                # 总分红 = price × shares
                if total_shares > 0 and shares > 0 and price > 0:
                    div_total = shares * price
                    total_cost -= div_total

        if total_shares <= 0:
            continue

        cost_basis = total_cost / total_shares if total_shares > 0 else 0
        avg_buy_price = total_buy_amount / total_buy_shares if total_buy_shares > 0 else 0
        cur_price = price_map.get(ticker)
        daily_chg = quote_map.get(ticker, {}).get("change_pct")
        market_value = cur_price * total_shares if cur_price else None
        pnl = (cur_price - cost_basis) * total_shares if cur_price else None
        pnl_pct = ((cur_price - cost_basis) / cost_basis * 100) if (cur_price and cost_basis) else None
        pct_of_total = (market_value / (total_capital * 10000) * 100) if (market_value and total_capital) else None

        # 币种由 quote 决定（quote dict 里有 "currency"），fallback 用 resolve_ticker
        currency = (quote_map.get(ticker) or {}).get("currency")
        if not currency:
            from bigv_twins.stock_data import resolve_ticker
            info = resolve_ticker(ticker)
            currency = info.currency if info else "CNY"

        portfolio.append({
            "ticker": ticker,
            "name": ticker_name_map.get(ticker, ticker),
            "currency": currency,
            "shares": total_shares,
            "cost_basis": cost_basis,
            "avg_buy_price": avg_buy_price,
            "current_price": cur_price,
            "daily_chg": daily_chg,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "pct_of_total": pct_of_total,
        })

    portfolio.sort(key=lambda x: abs(x.get("market_value") or 0), reverse=True)
    return portfolio


@router.get("", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    status_filter = request.query_params.get("status", "active")  # active / closed / all

    # 总是拉所有 journals（无视 filter）— 我们要算每个 tab 的 count，
    # 并在 closed 卡片上做完整 pnl 计算（buys + sells）
    rows = await session.execute(
        select(DecisionJournal)
        .where(DecisionJournal.user_id == user.id)
        .order_by(DecisionJournal.created_at)  # 升序：每个 ticker 内交易按时间往后铺
    )
    all_journals = list(rows.scalars())

    # 按 ticker 分组
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for j in all_journals:
        grouped[j.ticker].append(j)

    # 拉所有 ticker 的实时报价（active 计浮盈用）
    all_tickers = list(grouped.keys())
    price_map: dict[str, float] = {}
    quote_map: dict[str, dict] = {}
    if all_tickers:
        quotes = await asyncio.to_thread(
            get_watchlist_quotes, [_FakeW(t) for t in all_tickers]
        )
        for qq in quotes:
            if qq.get("ok") and qq.get("current"):
                price_map[qq["ticker"]] = qq["current"]
                quote_map[qq["ticker"]] = qq

    # 老的 portfolio 结构仍然要算（顶部 stat 卡片要用）
    all_active = [j for j in all_journals if j.status == "active"]
    portfolio = _build_portfolio(all_active, price_map, quote_map, None)
    portfolio_by_ticker = {p["ticker"]: p for p in portfolio}

    # === 按币种分两路统计 ===
    # 资金（principal/dividend）从 user 字段拿；市值/浮盈从 portfolio 按币种聚合
    accounts: dict[str, dict] = {
        "CNY": {
            "label": "A 股账户",
            "symbol": "¥",
            "principal": user.cny_principal or 0,
            "dividend": user.cny_dividend or 0,
            "market_value": 0.0,
            "unrealized_pnl": 0.0,
            "principal_field": "cny_principal",
            "dividend_field": "cny_dividend",
        },
        "HKD": {
            "label": "港股账户",
            "symbol": "HK$",
            "principal": user.hkd_principal or 0,
            "dividend": user.hkd_dividend or 0,
            "market_value": 0.0,
            "unrealized_pnl": 0.0,
            "principal_field": "hkd_principal",
            "dividend_field": "hkd_dividend",
        },
    }
    for p in portfolio:
        cur = p.get("currency") or "CNY"
        if cur in accounts:
            accounts[cur]["market_value"] += p.get("market_value") or 0
            accounts[cur]["unrealized_pnl"] += p.get("pnl") or 0
    # 总资产 = 总市值 + 现金（本金）+ 分红
    for acc in accounts.values():
        acc["total_assets"] = acc["market_value"] + acc["principal"] + acc["dividend"]

    # 投资随笔（拉全部，模板按时间归档）
    notes_q = select(InvestmentNote).where(
        InvestmentNote.user_id == user.id
    ).order_by(InvestmentNote.created_at.desc())
    notes_rows = await session.execute(notes_q)
    notes_all = list(notes_rows.scalars())

    # 按时间归档：当前月扁平展开；本年其他月份每月一组；跨年的整年一组
    today = date.today()
    this_year = today.year
    this_yyyymm = today.strftime("%Y-%m")
    current_notes = []
    month_groups: dict[str, list] = {}  # YYYY-MM -> notes (本年其他月份)
    year_groups: dict[int, list] = {}    # YYYY -> notes (跨年)
    for n in notes_all:
        if not n.created_at:
            continue
        ymd = n.created_at.strftime("%Y-%m")
        if ymd == this_yyyymm:
            current_notes.append(n)
        elif n.created_at.year == this_year:
            month_groups.setdefault(ymd, []).append(n)
        else:
            year_groups.setdefault(n.created_at.year, []).append(n)

    # month_groups → 按月份倒序的 list
    notes_month_sections = sorted(month_groups.items(), key=lambda x: x[0], reverse=True)
    # year_groups → 每年再按月分组
    notes_year_sections = []
    for yr in sorted(year_groups.keys(), reverse=True):
        ns = year_groups[yr]
        by_mo: dict[str, list] = {}
        for n in ns:
            by_mo.setdefault(n.created_at.strftime("%Y-%m"), []).append(n)
        sub = sorted(by_mo.items(), key=lambda x: x[0], reverse=True)
        notes_year_sections.append({"year": yr, "total": len(ns), "months": sub})

    # 拉每只 ticker 最新一份 AI 回顾（per-ticker）
    from .db import DecisionReview
    latest_reviews_q = await session.execute(
        select(DecisionReview)
        .where(
            DecisionReview.user_id == user.id,
            DecisionReview.ticker.isnot(None),
        )
        .order_by(DecisionReview.created_at.desc())
    )
    latest_review_per_ticker: dict[str, DecisionReview] = {}
    for r in latest_reviews_q.scalars():
        if r.ticker and r.ticker not in latest_review_per_ticker:
            latest_review_per_ticker[r.ticker] = r

    # === 给每只 ticker 算卡片显示需要的所有字段 ===
    ticker_cards = []
    total_realized_pnl = 0.0
    for ticker, entries in grouped.items():
        # entries 已经按 created_at 升序（DB 查询时排过）
        # any_active 只看真实持仓动作，忽略 dividend 入账（dividend 是历史
        # 事件，即便 ticker 当时已清仓也会被记录）
        any_active = any(
            e.status == "active" and e.action != "dividend" for e in entries
        )
        latest = entries[-1]

        # 已实现盈亏（不管 active 还是 closed 都算，因为 closed 周期内可能有 reduce）
        # 简化口径：sells (reduce + close) 总收入 − 已对应的 buys 成本（按 buy 比例分摊）
        buys = [e for e in entries if e.action in ("open", "add", "retroactive")]
        sells = [e for e in entries if e.action in ("reduce", "close")]
        total_buy_cost = sum((b.price_at_decision or 0) * (b.shares or 0) for b in buys)
        total_buy_shares = sum(b.shares or 0 for b in buys)
        avg_cost = (total_buy_cost / total_buy_shares) if total_buy_shares else 0
        realized_sold_shares = sum(s.shares or 0 for s in sells)
        realized_proceeds = sum((s.price_at_decision or 0) * (s.shares or 0) for s in sells)
        realized_pnl = realized_proceeds - avg_cost * realized_sold_shares if avg_cost else 0
        if realized_pnl:
            total_realized_pnl += realized_pnl

        # 币种：active 票从 quote 拿；closed 票从 ticker code 推断
        currency = (quote_map.get(ticker) or {}).get("currency")
        if not currency:
            from bigv_twins.stock_data import resolve_ticker
            info = resolve_ticker(ticker)
            currency = info.currency if info else "CNY"

        card = {
            "ticker": ticker,
            "ticker_name": latest.ticker_name,
            "currency": currency,
            "currency_symbol": "HK$" if currency == "HKD" else ("US$" if currency == "USD" else "¥"),
            "entries": entries,  # ascending order, oldest first
            "trade_count": len(entries),
            "any_active": any_active,
            "earliest_date": entries[0].created_at,
            "latest_date": latest.created_at,
            "realized_pnl": realized_pnl if sells else 0,
            "realized_pct": (realized_pnl / (avg_cost * realized_sold_shares) * 100) if avg_cost and realized_sold_shares else None,
            "latest_review": latest_review_per_ticker.get(ticker),  # 最新 AI 回顾（per-ticker）
        }

        if any_active:
            # 当前持仓信息（complex with reduces — reuse portfolio_by_ticker）
            p = portfolio_by_ticker.get(ticker)
            card.update({
                "current_shares": p["shares"] if p else 0,
                "cost_basis": p["cost_basis"] if p else None,           # 已实现盈亏调整后的剩余成本
                "avg_buy_price": p["avg_buy_price"] if p else None,    # 纯买入均价（不受卖出影响）
                "current_price": price_map.get(ticker),
                "market_value": p["market_value"] if p else 0,
                "unrealized_pnl": p["pnl"] if p else None,
                "unrealized_pct": p["pnl_pct"] if p else None,
            })
        else:
            # 已清仓
            close_action = next((e for e in reversed(entries) if e.action == "close"), None)
            close_date = close_action.created_at if close_action else (latest.created_at if latest else None)
            hold_days = (close_date - entries[0].created_at).days if (close_date and entries[0].created_at) else None
            card.update({
                "close_date": close_date,
                "hold_days": hold_days,
            })
        ticker_cards.append(card)

    # 各 tab count
    active_count = sum(1 for c in ticker_cards if c["any_active"])
    closed_count = sum(1 for c in ticker_cards if not c["any_active"])
    all_count = len(ticker_cards)

    # 按 status_filter 过滤
    if status_filter == "active":
        cards_view = [c for c in ticker_cards if c["any_active"]]
    elif status_filter == "closed":
        cards_view = [c for c in ticker_cards if not c["any_active"]]
    else:
        cards_view = ticker_cards[:]

    # 排序
    def _sort_key(c):
        if c["any_active"]:
            return (0, -(c.get("market_value") or 0))
        else:
            return (1, -(c["close_date"].timestamp() if c.get("close_date") else 0))
    cards_view.sort(key=_sort_key)

    return templates.TemplateResponse(
        request=request,
        name="journal/list.html",
        context={
            "user": user,
            "ticker_cards": cards_view,
            "active_count": active_count,
            "closed_count": closed_count,
            "all_count": all_count,
            "status_filter": status_filter,
            "accounts": accounts,  # CNY / HKD 各自 principal/dividend/mv/pnl/total_assets
            "total_realized_pnl": total_realized_pnl,
            "current_notes": current_notes,
            "notes_month_sections": notes_month_sections,
            "notes_year_sections": notes_year_sections,
        },
    )


@router.post("/capital")
async def set_capital(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    mode: str = Form("set"),                   # set / deposit / withdraw
    field: str = Form("cny_principal"),        # cny_principal / cny_dividend / hkd_principal / hkd_dividend
    amount: float = Form(None),                # 单位：元（包括港币元）
):
    """修改某币种的本金 or 分红入账。amount 为 None 时仅记录日志返回。"""
    if field not in ("cny_principal", "cny_dividend", "hkd_principal", "hkd_dividend"):
        raise HTTPException(status_code=400, detail="invalid field")
    if amount is None:
        return RedirectResponse("/journal", status_code=303)

    user_obj = await session.get(User, user.id)
    cur = getattr(user_obj, field) or 0
    if mode == "set":
        new = amount
    elif mode == "deposit":
        new = cur + amount
    elif mode == "withdraw":
        new = max(0, cur - amount)
    else:
        raise HTTPException(status_code=400, detail="invalid mode")
    setattr(user_obj, field, new)
    log.info("set_capital: user=%s field=%s mode=%s old=%s new=%s",
             user.id, field, mode, cur, new)
    await session.commit()
    return RedirectResponse("/journal", status_code=303)


@router.get("/new", response_class=HTMLResponse)
async def journal_create_form(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
):
    ticker = request.query_params.get("ticker", "")
    name = request.query_params.get("name", "")
    from datetime import datetime
    now_iso = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request=request,
        name="journal/create.html",
        context={
            "user": user, "prefill_ticker": ticker, "prefill_name": name,
            "now_iso": now_iso,
        },
    )


@router.post("/new")
async def journal_create(
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    ticker: str = Form(...),
    ticker_name: str = Form(...),
    action: str = Form(...),
    price_at_decision: float = Form(None),
    shares: int = Form(None),
    reasoning: str = Form(""),
    action_plan: str = Form(""),
    target_price: float = Form(None),
    stop_loss_price: float = Form(None),
    expected_hold_period: str = Form(""),
    if_drop_10pct: str = Form(""),
    decision_at: str = Form(""),
    market_hint: str = Form("a-share"),  # a-share / hk — 决定按名字搜索时去哪个名单
):
    # Resolve ticker: ALWAYS normalize to code. ticker_name only stores the name.
    raw_ticker = ticker.strip()
    raw_name = ticker_name.strip()
    # 用户的市场提示：港股的话强制走 HK 解析；A 股 ticker 自动按代码长度判定 ETF
    if market_hint == "hk":
        # 港股：直接拿 5 位代码或 .HK 后缀去 resolve
        info = None
        if raw_ticker:
            info = resolve_ticker(raw_ticker)  # 5 位代码会走 HK 分支
        # 如果用户只填了名字，目前 resolve_ticker 没有港股名→码的反查，
        # 退化成 raw_name 当代码（多半会失败 — 用户应该填代码）
    else:
        info = None
        if raw_ticker:
            info = resolve_ticker(raw_ticker)
        if not info and raw_name:
            info = resolve_ticker(raw_name)
    if info:
        raw_ticker = info.code
        raw_name = info.name
    if not raw_name:
        raw_name = raw_ticker

    # Parse decision_at (HTML5 date: "2026-05-29" → midnight)
    created_at = None
    if decision_at:
        try:
            from datetime import datetime
            created_at = datetime.fromisoformat(decision_at)
        except ValueError:
            pass

    journal = DecisionJournal(
        user_id=user.id,
        ticker=raw_ticker,
        ticker_name=raw_name,
        action=action,
        action_detail=action_plan or None,
        price_at_decision=price_at_decision,
        position_pct=None,
        shares=shares,
        reasoning=reasoning or None,
        hold_conditions=None,
        exit_signals=None,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        expected_hold_period=expected_hold_period or None,
        if_drop_10pct=if_drop_10pct or None,
        status="active",
        **({"created_at": created_at} if created_at else {}),
    )
    session.add(journal)
    await session.flush()
    journal_id = journal.id
    await session.commit()
    background_tasks.add_task(_fill_snapshot, journal_id)
    # Auto-add to watchlist
    background_tasks.add_task(_auto_add_watchlist, user.id, ticker.strip(), ticker_name.strip())
    return RedirectResponse(f"/journal/{journal_id}", status_code=303)


@router.get("/{jid}", response_class=HTMLResponse)
async def journal_detail(
    request: Request,
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)

    quotes = await asyncio.to_thread(get_watchlist_quotes, [_FakeW(journal.ticker)])
    current_price = quotes[0].get("current") if quotes else None
    pnl_pct = None
    if current_price and journal.price_at_decision:
        pnl_pct = (current_price - journal.price_at_decision) / journal.price_at_decision * 100

    snapshot = json.loads(journal.stock_snapshot) if journal.stock_snapshot else None
    opinions = json.loads(journal.blogger_opinions) if journal.blogger_opinions else []
    # v0.7: 不再 fetch per-journal reviews — 回顾改 per-ticker，在 /stock/{ticker} 看

    return templates.TemplateResponse(
        request=request,
        name="journal/detail.html",
        context={
            "user": user,
            "j": journal,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "snapshot": snapshot,
            "opinions": opinions,
        },
    )




@router.get("/{jid}/edit", response_class=HTMLResponse)
async def journal_edit_form(
    request: Request,
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    decision_at_iso = (
        journal.created_at.strftime("%Y-%m-%d") if journal.created_at else ""
    )
    return templates.TemplateResponse(
        request=request,
        name="journal/edit.html",
        context={"user": user, "j": journal, "decision_at_iso": decision_at_iso},
    )


@router.post("/{jid}/edit")
async def journal_edit(
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    action: str = Form(...),
    price_at_decision: float = Form(None),
    shares: int = Form(None),
    reasoning: str = Form(""),
    action_plan: str = Form(""),
    target_price: float = Form(None),
    stop_loss_price: float = Form(None),
    expected_hold_period: str = Form(""),
    if_drop_10pct: str = Form(""),
    decision_at: str = Form(""),
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    # ticker/ticker_name 锁死不可改（避免数据错乱）— 表单中是 readonly 不接收
    journal.action = action
    journal.price_at_decision = price_at_decision
    journal.shares = shares
    journal.reasoning = reasoning or None
    journal.action_detail = action_plan or None
    journal.target_price = target_price
    journal.stop_loss_price = stop_loss_price
    journal.expected_hold_period = expected_hold_period or None
    journal.if_drop_10pct = if_drop_10pct or None
    if decision_at:
        try:
            from datetime import datetime
            journal.created_at = datetime.fromisoformat(decision_at)
        except ValueError:
            pass
    await session.commit()
    return RedirectResponse(f"/journal/{jid}", status_code=303)


@router.post("/{jid}/quick-edit")
async def journal_quick_edit(
    request: Request,
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    field: str = Form(...),  # 'reasoning' or 'action_plan'
    value: str = Form(""),
    redirect_to: str = Form("/journal"),
):
    """单字段快速编辑（思路/计划），用于 /stock 等页面 inline 编辑。"""
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    cleaned = value.strip() or None
    if field == "reasoning":
        journal.reasoning = cleaned
    elif field == "action_plan":
        journal.action_detail = cleaned
    else:
        raise HTTPException(status_code=400, detail="unknown field")
    await session.commit()
    return RedirectResponse(redirect_to, status_code=303)


_CRITIQUE_DATE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$", re.S)


def _parse_critique(text: str | None) -> list[tuple[str, str]]:
    """把 self_critique 解析为 [(date_iso_or_'legacy', content), ...]。

    新格式每段以 [YYYY-MM-DD] 开头。老数据（裸文本或老的「[M月D日 追加评价]」
    格式）按段落保留为 legacy 块，不丢内容。
    """
    if not text or not text.strip():
        return []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    out: list[tuple[str, str]] = []
    for blk in blocks:
        m = _CRITIQUE_DATE_RE.match(blk)
        if m:
            out.append((m.group(1), m.group(2).strip()))
        else:
            out.append(("legacy", blk))
    return out


def _serialize_critique(entries: list[tuple[str, str]]) -> str:
    pieces = []
    for d, c in entries:
        if d == "legacy":
            pieces.append(c)
        else:
            pieces.append(f"[{d}] {c}")
    return "\n\n".join(pieces)


def _append_critique(existing: str | None, new_text: str) -> str:
    """追加一条评价。同一天的多次评价合并到当日 block，用「；」分隔。"""
    today = date.today().strftime("%Y-%m-%d")
    entries = _parse_critique(existing)
    if entries and entries[-1][0] == today:
        d, c = entries[-1]
        entries[-1] = (d, c + "；" + new_text)
    else:
        entries.append((today, new_text))
    return _serialize_critique(entries)


@router.post("/{jid}/critique")
async def journal_critique(
    request: Request,
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    critique: str = Form(...),
    redirect_to: str = Form("/journal"),
):
    """追加一条自评。每条带 [YYYY-MM-DD] 日期前缀，同日多次合并。"""
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    new_text = critique.strip()
    if not new_text:
        return RedirectResponse(redirect_to, status_code=303)
    journal.self_critique = _append_critique(journal.self_critique, new_text)
    await session.commit()
    return RedirectResponse(redirect_to, status_code=303)


@router.post("/{jid}/close")
async def journal_close(
    jid: int,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    closed_price: float = Form(...),
    closed_reason: str = Form(""),
    closed_shares: int = Form(None),
):
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    from datetime import datetime

    # Create a new "close" journal entry as a separate trade record
    close_entry = DecisionJournal(
        user_id=user.id,
        ticker=journal.ticker,
        ticker_name=journal.ticker_name,
        action="close",
        action_detail=closed_reason or None,
        price_at_decision=closed_price,
        shares=closed_shares or journal.shares,
        reasoning=closed_reason or "清仓",
        status="active",
    )
    session.add(close_entry)

    # Mark ALL active entries for this ticker as closed
    all_rows = await session.execute(
        select(DecisionJournal).where(
            DecisionJournal.user_id == user.id,
            DecisionJournal.ticker == journal.ticker,
            DecisionJournal.status == "active",
        )
    )
    for j in all_rows.scalars():
        j.status = "closed"
        j.closed_at = datetime.now()
        j.closed_price = closed_price
        j.closed_reason = closed_reason or None

    # 清仓后：如果自选股是因为交易自动加的，自动移除；手动加的保留
    wl_row = await session.execute(
        select(UserWatchlist).where(
            UserWatchlist.user_id == user.id,
            UserWatchlist.ticker == journal.ticker,
        )
    )
    wl = wl_row.scalar_one_or_none()
    if wl and wl.added_via == "auto":
        await session.delete(wl)

    await session.commit()
    return RedirectResponse("/journal", status_code=303)




@router.post("/{jid}/review/now")
async def manual_review_redirect(
    jid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    """Legacy 路由 — v0.7 起回顾改 per-ticker，转发到 /stock/{ticker}/review/now。"""
    journal = await session.get(DecisionJournal, jid)
    if not journal or journal.user_id != user.id:
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/stock/{journal.ticker}/review/now", status_code=307)


@router.post("/note")
async def create_note(
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    content: str = Form(...),
):
    note = InvestmentNote(user_id=user.id, content=content.strip())
    session.add(note)
    await session.commit()
    return RedirectResponse("/journal#notes", status_code=303)


@router.post("/note/{nid}/edit")
async def edit_note(
    nid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
    content: str = Form(...),
):
    """允许修改随笔内容（改错别字之类）。不允许删除 — 随笔是回测/AI 复盘
    的素材，删了就丢了。"""
    note = await session.get(InvestmentNote, nid)
    if not note or note.user_id != user.id:
        raise HTTPException(status_code=404)
    cleaned = content.strip()
    if cleaned:
        note.content = cleaned
        await session.commit()
    return RedirectResponse("/journal#notes", status_code=303)
