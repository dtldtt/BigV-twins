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
from .blogger_brief import generate_briefs_for_day
from .news_scraper import refresh_jin10_news
from .report import router as report_router
from .search import rebuild_search_index, router as search_router
from .ticker_brief import generate_ticker_briefs_for_day


PKG_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PKG_DIR / "templates"))


async def _refresh_jin10_and_index() -> None:
    """jin10 拉新 + 增量重建搜索索引（FTS）。"""
    await refresh_jin10_news()
    await rebuild_search_index()


async def _generate_blogger_briefs_and_index() -> None:
    await generate_briefs_for_day()
    await rebuild_search_index()


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
    scheduler.add_job(_refresh_jin10_and_index, IntervalTrigger(hours=4), id="jin10_news",
                      misfire_grace_time=600, replace_existing=True)
    scheduler.add_job(_refresh_jin10_and_index, "date", id="jin10_news_initial",
                      replace_existing=True)
    # Daily blogger brief at 03:30 (after zhihu daily timer 03:01 + bigv-twins
    # daily indexer 03:21). Cron in Asia/Shanghai timezone.
    scheduler.add_job(_generate_blogger_briefs_and_index, CronTrigger(hour=3, minute=30),
                      id="blogger_brief_daily",
                      misfire_grace_time=3600, replace_existing=True)
    # Per-ticker brief refreshed 2x daily; UPSERT same-day row
    #   08:00 — 昨日收盘+隔夜消息  19:00 — 当日全天数据（收盘后 +1h）
    for hh, mm, jid in ((8, 0, "morning"), (19, 0, "evening")):
        scheduler.add_job(_generate_ticker_briefs_and_index,
                          CronTrigger(hour=hh, minute=mm),
                          id=f"ticker_brief_{jid}",
                          misfire_grace_time=1800, replace_existing=True)
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
    app.include_router(search_router)
    app.include_router(admin_router)
    app.include_router(about_router)

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
