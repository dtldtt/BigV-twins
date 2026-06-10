"""FastAPI app entry for the 赛博大V chat UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bigv_twins.config import settings

from . import auth, db
from .about import router as about_router
from .admin import router as admin_router
from .auth_routes import router as auth_router
from .chat import router as chat_router
from .db import User
from .multi import router as multi_router
from .backtest import about_track_router, compute_all_entries, router as backtest_router
from .blogger_brief import generate_briefs_for_day
from .digest import generate_daily_digest
from .trends import router as trends_router, extract_predictions_from_digest, save_market_snapshot
from .persona_updater import run_monthly_persona_update
from .closed_review import run_monthly_closed_reviews
from .news_scraper import refresh_jin10_news
from .report import router as report_router
from .search import rebuild_search_index, router as search_router
from .journal import router as journal_router
from .stock import router as stock_router
from .consensus import router as consensus_router
from .growth import router as growth_router
from .reflection_engine import run_monthly_growth_reports, run_quarterly_growth_reports
from .timeline import router as timeline_router
from .review_engine import run_scheduled_reviews
from .dividend_sync import sync_all_users_dividends
from .token_usage import refresh_token_usage
from .ticker_brief import generate_ticker_briefs_for_day


PKG_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PKG_DIR / "templates"))

def _fromjson_filter(s):
    """Jinja2 filter: parse JSON string. Returns {} on error."""
    if not s:
        return {}
    if isinstance(s, (dict, list)):
        return s
    import json as _json
    try:
        return _json.loads(s)
    except (TypeError, ValueError, _json.JSONDecodeError):
        return {}

TEMPLATES.env.filters['fromjson'] = _fromjson_filter


async def _refresh_jin10_and_index() -> None:
    """jin10 拉新 + 增量重建搜索索引（FTS）。"""
    await refresh_jin10_news()
    await rebuild_search_index()


async def _generate_blogger_briefs_and_index() -> None:
    await generate_briefs_for_day()
    await rebuild_search_index()
    # Also compute backtest entries for any new ticker mentions
    try:
        await compute_all_entries()
    except Exception as e:
        logging.getLogger("bigv_twins.web").warning("backtest compute failed: %s", e)


async def _generate_ticker_briefs_and_index() -> None:
    await generate_ticker_briefs_for_day()
    await rebuild_search_index()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # Bootstrap search index from existing data
    try:
        counts = await rebuild_search_index()
        logging.getLogger("bigv_twins.web").info("search index bootstrapped: %s", counts)
    except Exception as e:
        logging.getLogger("bigv_twins.web").warning(
            "search index bootstrap failed (continuing): %s", e
        )
    # APScheduler — periodic background jobs (jin10 refresh / daily blogger brief).
    # Each batch job has a wrapper that ALSO rebuilds the FTS search index
    # afterwards, so /search stays fresh without manual rebuild.
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    # Refresh jin10 news every 4 hours; also kick off once at startup so a
    # freshly-restarted server isn't empty
    scheduler.add_job(_refresh_jin10_and_index, IntervalTrigger(hours=2), id="jin10_news",
                      misfire_grace_time=600, replace_existing=True)
    scheduler.add_job(_refresh_jin10_and_index, "date", id="jin10_news_initial",
                      replace_existing=True)
    # Daily blogger brief at 03:30 (after zhihu daily timer 03:01 + bigv-twins
    # daily indexer 03:21). Cron in Asia/Shanghai timezone.
    scheduler.add_job(_generate_blogger_briefs_and_index, CronTrigger(hour=3, minute=30),
                      id="blogger_brief_daily",
                      misfire_grace_time=3600, replace_existing=True)
    # Daily digest at 03:40 (after blogger briefs finish ~03:35)
    scheduler.add_job(generate_daily_digest, CronTrigger(hour=3, minute=40),
                      id="daily_digest",
                      misfire_grace_time=3600, replace_existing=True)
    # 03:45 — digest 完成后提取可验证预测
    async def _extract_predictions():
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        await extract_predictions_from_digest(yesterday)
    scheduler.add_job(_extract_predictions, CronTrigger(hour=3, minute=45),
                      id="extract_predictions",
                      misfire_grace_time=3600, replace_existing=True)
    # 16:00 — 收盘后存行情快照
    scheduler.add_job(save_market_snapshot, CronTrigger(hour=16, minute=0),
                      id="market_snapshot",
                      misfire_grace_time=3600, replace_existing=True)
    # Per-ticker brief refreshed 2x daily; UPSERT same-day row
    #   08:00 — 昨日收盘+隔夜消息  19:00 — 当日全天数据（收盘后 +1h）
    for hh, mm, jid in ((8, 0, "morning"), (19, 0, "evening")):
        scheduler.add_job(_generate_ticker_briefs_and_index,
                          CronTrigger(hour=hh, minute=mm),
                          id=f"ticker_brief_{jid}",
                          misfire_grace_time=1800, replace_existing=True)
    # Decision review — daily at 20:00
    # Per-ticker AI 回顾 — 每周六 20:00 跑（per-ticker，不再按 7→30→90→180 阶梯）
    # 改成固定周节奏，每周一份新报告，旧报告按时间倒序保留
    scheduler.add_job(run_scheduled_reviews,
                      CronTrigger(day_of_week="sat", hour=20, minute=0),
                      id="weekly_ticker_reviews", misfire_grace_time=3600, replace_existing=True)

    # A 股分红自动同步 — 每日 17:30（A 股收盘后），拉每个 user 的所有 A 股 ticker
    # 历史分红，找持仓期内已实施的事件，自动入账
    scheduler.add_job(sync_all_users_dividends, CronTrigger(hour=17, minute=30),
                      id="dividend_sync", misfire_grace_time=3600, replace_existing=True)

    # Persona 月度更新 — 每月 1 号 06:00
    scheduler.add_job(run_monthly_persona_update, CronTrigger(day=1, hour=6, minute=0),
                      id="persona_monthly_update", misfire_grace_time=7200, replace_existing=True)
    # 已清仓标的月度复盘 — 每月 1 号 07:00
    scheduler.add_job(run_monthly_closed_reviews, CronTrigger(day=1, hour=7, minute=0),
                      id="closed_review_monthly", misfire_grace_time=7200, replace_existing=True)
    # 成长复盘 — 月度（每月 1 号 09:00 跑上月）+ 季度（1/4/7/10 月 1 号 09:30 跑上季）
    scheduler.add_job(run_monthly_growth_reports, CronTrigger(day=1, hour=9, minute=0),
                      id="growth_monthly", misfire_grace_time=3600, replace_existing=True)
    scheduler.add_job(run_quarterly_growth_reports,
                      CronTrigger(month="1,4,7,10", day=1, hour=9, minute=30),
                      id="growth_quarterly", misfire_grace_time=3600, replace_existing=True)

    # Token usage tracker — hourly (no LLM, just scan jsonl)
    scheduler.add_job(refresh_token_usage, IntervalTrigger(hours=1),
                      id="token_usage_refresh", misfire_grace_time=600, replace_existing=True)
    scheduler.add_job(refresh_token_usage, "date", id="token_usage_initial",
                      replace_existing=True)

    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    if not settings.web_secret_key:
        raise RuntimeError(
            "WEB_SECRET_KEY is empty in .env — generate one with "
            "`openssl rand -hex 32` and add it."
        )

    app = FastAPI(title="赛博大V", lifespan=lifespan)

    # 默认 FastAPI 的 ServerErrorMiddleware 会吞掉 traceback 只返回 generic
    # 错误页，systemd journal 里就看不到具体哪一行炸了（之前 /stock/603369
    # 的 AttributeError 就是这么藏起来一年的）。这里手动 logger.exception
    # 把 traceback 打到 stderr → journalctl 看得到。
    @app.exception_handler(Exception)
    async def log_and_reraise(request: Request, exc: Exception):
        from fastapi.responses import PlainTextResponse
        logging.getLogger("bigv_twins.web").exception(
            "unhandled exception on %s %s", request.method, request.url.path
        )
        return PlainTextResponse("Internal Server Error", status_code=500)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web_secret_key,
        session_cookie="bigv_session",
        max_age=86400 * 7,
        same_site="lax",
        https_only=False,
    )

    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(multi_router)
    app.include_router(report_router)
    app.include_router(trends_router)            # /report/trends
    app.include_router(backtest_router)         # /report/leaderboard etc.
    app.include_router(about_track_router)      # /about/<slug>/track-record
    app.include_router(search_router)
    app.include_router(admin_router)
    app.include_router(about_router)
    app.include_router(journal_router)
    app.include_router(stock_router)
    app.include_router(timeline_router)
    app.include_router(consensus_router)
    app.include_router(growth_router)

    @app.get("/changelog", response_class=HTMLResponse)
    async def changelog_page(
        request: Request,
        user: Annotated[User | None, Depends(auth.current_user)],
    ):
        return TEMPLATES.TemplateResponse(request=request, name="changelog.html", context={"user": user})

    @app.get("/", response_class=HTMLResponse)
    async def home(
        request: Request,
        user: Annotated[User | None, Depends(auth.current_user)],
    ):
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/chat", status_code=303)

    return app


app = create_app()


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    uvicorn.run(
        "bigv_twins.web.app:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
