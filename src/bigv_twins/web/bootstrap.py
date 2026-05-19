"""One-shot CLI to create the first admin user.

Usage:
    python -m bigv_twins.web.bootstrap
    # or, after `pip install -e .`:
    bigv-twins-web-bootstrap

Reads username/password from stdin (or env BIGV_ADMIN_USERNAME / BIGV_ADMIN_PASSWORD
if both set). Refuses to run if an admin user already exists.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from . import auth, db
from .db import User


def _read_credentials() -> tuple[str, str]:
    env_user = os.environ.get("BIGV_ADMIN_USERNAME")
    env_pw = os.environ.get("BIGV_ADMIN_PASSWORD")
    if env_user and env_pw:
        print(f"Using BIGV_ADMIN_USERNAME / _PASSWORD from environment for {env_user!r}.")
        return env_user.strip(), env_pw

    print("Creating the FIRST admin user for 赛博大V.")
    print()
    while True:
        username = input("Admin username (3-32 chars, alnum/-_.): ").strip()
        if (3 <= len(username) <= 32) and all(c.isalnum() or c in "-_." for c in username):
            break
        print("  invalid — try again.")

    while True:
        pw1 = getpass.getpass("Admin password (>= 8 chars): ")
        if len(pw1) < 8:
            print("  too short — try again.")
            continue
        pw2 = getpass.getpass("Re-enter password: ")
        if pw1 != pw2:
            print("  mismatch — try again.")
            continue
        break

    return username, pw1


async def _main() -> int:
    await db.init_db()

    async with db._SessionFactory() as session:
        existing = await session.execute(select(User).where(User.role == "admin"))
        if existing.scalar_one_or_none() is not None:
            print("ERROR: an admin user already exists; refusing to bootstrap.", file=sys.stderr)
            return 1

        username, password = _read_credentials()

        clash = await session.execute(select(User).where(User.username == username))
        if clash.scalar_one_or_none() is not None:
            print(f"ERROR: username {username!r} already taken.", file=sys.stderr)
            return 1

        user = User(
            username=username,
            password_hash=auth.hash_password(password),
            role="admin",
            invite_id=None,
        )
        session.add(user)
        await session.commit()
        print(f"OK: admin user {username!r} created.")
        return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
