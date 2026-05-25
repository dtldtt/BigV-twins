"""一次性回填脚本：补齐过去 N 天每位博主的 daily brief。

调用 generate_briefs_for_day(day_str) 逐天跑；同 (slug, brief_date) 已存在则跳过
（idempotent）。

用法：
  python -m bigv_twins.web.backfill_blogger_briefs [--days 30] [--start YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import date, timedelta

from bigv_twins.web.blogger_brief import generate_briefs_for_day


async def main(days: int, start: date) -> None:
    log = logging.getLogger("backfill")
    total_gen = total_skip = total_err = 0
    t0 = time.time()
    log.info("backfill: start=%s, days=%d (going backward from start)", start, days)

    for offset in range(days):
        d = start - timedelta(days=offset)
        day_str = d.strftime("%Y-%m-%d")
        log.info("=== day %d/%d: %s ===", offset + 1, days, day_str)
        try:
            r = await generate_briefs_for_day(day_str)
        except Exception as e:
            log.exception("day %s failed: %s", day_str, e)
            total_err += 1
            continue
        total_gen += r.get("generated", 0)
        total_skip += r.get("skipped_existing", 0)
        total_err += r.get("errors", 0)
        log.info("  → gen=%d skip=%d err=%d", r.get("generated", 0),
                 r.get("skipped_existing", 0), r.get("errors", 0))

    elapsed = time.time() - t0
    log.info("=" * 50)
    log.info("BACKFILL DONE in %.1fs (%.1f min)", elapsed, elapsed / 60)
    log.info("  total generated:        %d", total_gen)
    log.info("  total skipped existing: %d", total_skip)
    log.info("  total errors:           %d", total_err)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start", type=str, default=None,
                   help="YYYY-MM-DD; defaults to yesterday")
    args = p.parse_args()

    if args.start is None:
        start = date.today() - timedelta(days=1)
    else:
        start = date.fromisoformat(args.start)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(main(args.days, start))
