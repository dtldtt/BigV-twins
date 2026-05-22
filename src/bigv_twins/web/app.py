"""FastAPI app entry for the 赛博大V chat UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

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
from .report import router as report_router


PKG_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PKG_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


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
