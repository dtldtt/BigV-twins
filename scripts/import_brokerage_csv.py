"""Import brokerage transaction CSV into decision_journal.

CSV columns: 成交日期,成交时间,代码,名称,交易类别,成交数量,成交价格,发生金额,成交金额,费用,备注
- Skip 转入 (deposits) and 除权除息 (dividend payouts)
- Process buy/sell in chronological order
- Classify: first buy=open, subsequent buy=add, sell with remaining=reduce, sell to zero=close
- Auto-manage watchlist with added_via='auto'

Run: python /tmp/import_csv.py [--dry-run]
"""
import asyncio
import csv
import sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, "/home/dtl/projects/BigV-twins/src")

from sqlalchemy import select, delete
from bigv_twins.web.db import (
    _SessionFactory, DecisionJournal, UserWatchlist,
)

CSV_PATH = "/tmp/transaction-record.csv"
USER_ID = 1  # dtl
DRY = "--dry-run" in sys.argv


def parse_rows():
    """Read CSV, filter buy/sell, sort by datetime."""
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            tx_type = r.get("交易类别", "").strip()
            if tx_type not in ("买入", "卖出"):
                continue
            date_str = r["成交日期"].strip()
            time_str = (r.get("成交时间") or "").strip() or "00:00:00"
            # 成交时间 may be HH:MM:SS or empty
            if len(time_str) == 5:
                time_str += ":00"
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            rows.append({
                "dt": dt,
                "ticker": r["代码"].strip(),
                "name": r["名称"].strip(),
                "type": tx_type,
                "shares": int(float(r["成交数量"])),
                "price": float(r["成交价格"]),
            })
    rows.sort(key=lambda x: x["dt"])
    return rows


def classify(rows):
    """Add action field to each row based on running balance."""
    balance: dict[str, int] = defaultdict(int)
    for r in rows:
        ticker = r["ticker"]
        if r["type"] == "买入":
            r["action"] = "open" if balance[ticker] == 0 else "add"
            balance[ticker] += r["shares"]
        else:  # 卖出
            balance[ticker] -= r["shares"]
            if balance[ticker] <= 0:
                r["action"] = "close"
                balance[ticker] = 0
            else:
                r["action"] = "reduce"
    return rows


async def import_rows(rows):
    """Insert each row into DB, manage watchlist."""
    n_insert = 0
    n_wl_add = 0
    n_wl_del = 0
    async with _SessionFactory() as s:
        # Track which tickers we've already touched (for watchlist add)
        seen_tickers: set[str] = set()

        for r in rows:
            ticker = r["ticker"]
            action = r["action"]

            entry = DecisionJournal(
                user_id=USER_ID,
                ticker=ticker,
                ticker_name=r["name"],
                action=action,
                price_at_decision=r["price"],
                shares=r["shares"],
                reasoning="",  # 空，后期可补
                status="active",
                created_at=r["dt"],
            )
            s.add(entry)
            n_insert += 1

            if action == "open":
                # First trade for this ticker — auto-add to watchlist
                wl_row = await s.execute(
                    select(UserWatchlist).where(
                        UserWatchlist.user_id == USER_ID,
                        UserWatchlist.ticker == ticker,
                    )
                )
                if not wl_row.scalar_one_or_none():
                    market = "HK" if len(ticker) == 5 else "A"
                    wl = UserWatchlist(
                        user_id=USER_ID, ticker=ticker, name=r["name"],
                        market=market, note="", added_via="auto",
                        added_at=r["dt"],
                    )
                    s.add(wl)
                    n_wl_add += 1
                seen_tickers.add(ticker)

            if action == "close":
                # Mark all prior active entries for this ticker as closed
                # (Include the close entry itself by flushing first.)
                await s.flush()
                from sqlalchemy import update
                await s.execute(
                    update(DecisionJournal)
                    .where(
                        DecisionJournal.user_id == USER_ID,
                        DecisionJournal.ticker == ticker,
                        DecisionJournal.status == "active",
                    )
                    .values(
                        status="closed",
                        closed_at=r["dt"],
                        closed_price=r["price"],
                    )
                )
                # Auto-remove watchlist if added_via='auto'
                wl_row = await s.execute(
                    select(UserWatchlist).where(
                        UserWatchlist.user_id == USER_ID,
                        UserWatchlist.ticker == ticker,
                    )
                )
                wl = wl_row.scalar_one_or_none()
                if wl and wl.added_via == "auto":
                    await s.delete(wl)
                    n_wl_del += 1

        if DRY:
            await s.rollback()
            print(f"[DRY] Would insert {n_insert} journal rows, "
                  f"add {n_wl_add} watchlist, delete {n_wl_del}")
        else:
            await s.commit()
            print(f"Inserted {n_insert} journal rows, "
                  f"added {n_wl_add} watchlist, deleted {n_wl_del}")


async def main():
    rows = parse_rows()
    print(f"Parsed {len(rows)} buy/sell rows from CSV")
    rows = classify(rows)

    # Action distribution
    counts = defaultdict(int)
    for r in rows:
        counts[r["action"]] += 1
    print(f"Actions: {dict(counts)}")

    # Final per-ticker balance preview
    balance = defaultdict(int)
    for r in rows:
        if r["action"] in ("open", "add"):
            balance[r["ticker"]] += r["shares"]
        else:
            balance[r["ticker"]] -= r["shares"]
            if balance[r["ticker"]] < 0:
                balance[r["ticker"]] = 0
    open_positions = {t: s for t, s in balance.items() if s > 0}
    print(f"Currently open positions: {len(open_positions)} tickers")
    for t, s in list(open_positions.items())[:5]:
        print(f"  {t}: {s} shares")

    await import_rows(rows)


asyncio.run(main())
