"""FastMCP server: 市场行情工具 (stock snapshot + market context).

The "neutral" half of BigV-twins MCP — provides real-time stock fundamentals
and macro/sector market context. Both `bigv` (blogger role-play) and `advisor`
(generic AI investment analyst) agents use these.

Listens on `MCP_MARKET_PORT` (default 8771).
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .config import settings
from .market_data import get_market_context as _get_market_context
from .stock_data import (
    get_dividend_history as _get_dividend_history,
    get_stock_snapshot as _get_stock_snapshot,
)

log = logging.getLogger("bigv_twins.market_server")

mcp = FastMCP(
    "bigv-market",
    instructions=(
        "Real-time market data tools. `get_stock_snapshot(query)` fetches "
        "current valuation / market cap / ownership / sector / index context "
        "for a specific stock (A-share / HK / US). `get_market_context(topics)` "
        "fetches recent (1w + 1m) performance for macro topics like 港股 / 黄金 / "
        "煤炭 / AI. `get_dividend_history(query)` fetches the past N dividend "
        "events + TTM yield for an A-share. All read-only, no LLM."
    ),
    host=settings.mcp_host,
    port=settings.mcp_market_port,
)


@mcp.tool()
def get_market_context(
    topics: Annotated[
        list[str],
        Field(description="主题 id 列表（白名单见 topics.json），如 ['a-share', 'hk', 'gold', 'industry-coal']"),
    ],
) -> dict:
    """获取若干主题的近期市场行情（1 周 + 1 月走势），用于宏观/板块/资产类讨论的背景参考。

    支持的 topic id（见 topics.json，可热修改）：
    - 大盘指数：`a-share` / `gem` / `star` / `bse` / `hk` / `us`
    - 资产类：`gold`
    - 行业 ETF：`industry-bank` / `industry-baijiu` / `industry-coal` / `industry-lithium`
                 `industry-semi` / `industry-ai` / `industry-new-energy` / `industry-military`
                 `industry-consumer` / `industry-real-estate` / `industry-resources`

    每个 topic 返回若干 asset 的最近 1 周和 1 月走势（如有历史），或仅 real-time spot（如港股指数 backup 路径）。

    注：web 入口已在 prompt 组装阶段对常见关键词（"港股"/"黄金"/"大盘"等）做了**预扫描自动召回**——
    所以大多数情况你不需要主动调这个工具。**当用户提到新主题、或你判断需要补充另一个主题的背景时**才调。
    """
    return _get_market_context(topics)


@mcp.tool()
def get_stock_snapshot(
    query: Annotated[
        str,
        Field(description="股票代码 (如 600519/300750/688981/00700) 或常见股票名 (如 茅台/宁德时代/腾讯)"),
    ],
) -> dict:
    """获取股票的基本面快照 + 大盘环境，用于辅助分析具体标的。

    返回内容（best-effort，部分字段在某些股票上可能缺失）：
    - resolved: 解析后的代码、名称、市场、板块（主板/创业板/科创板/北交所/港股）
    - price: 现价 / 当日涨跌% / 52周高低 / 最近 1 年涨跌%
    - valuation: PE_TTM / PB
    - scale: 总市值（亿，显示形式如「1.93 万亿」）
    - ownership: 控股性质（央企控股/省属国资控股/民营企业/外资企业...）+ 实际控制人
    - business: 行业 + 主营业务文字描述
    - company: 公司全名 / 董事长 / 员工数
    - index_context: 上证指数最近 10 天；如果是创业板再附创业板指；科创板再附科创50

    **务必**在用户问到具体股票/标的时**先**调用此工具拿到真实数字，
    然后再做分析——这样才能把方法论 / 量化阈值跟股票的真实数字逐条对照。

    数据源（按可靠性）：Tencent 实时报价 → 同花顺主营业务 → 雪球控股结构 → 新浪 K 线。
    缓存 10 分钟，重复查询同一股票不再请求外部 API。
    """
    return _get_stock_snapshot(query)


@mcp.tool()
def get_dividend_history(
    query: Annotated[
        str,
        Field(description="A 股代码 (如 601318) 或常见股票名 (如 中国平安/茅台)"),
    ],
    last_n: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """获取 A 股的分红派息历史 + 滚动 12 月分红总额 + 当前股息率。

    用户问到「X 的分红」「X 的股息率」「X 最近一次分红多少」「X 历年分红」时**先调这个**。
    `get_stock_snapshot` 只给 PE/PB/市值这种实时基本面，不含分红——分红需要专门拉。

    返回字段：
    - resolved: 解析后的代码 / 名称
    - history: 最近 last_n 条分红事件，**最新在前**。每条字段：
        - announce_date: 公告日期 (YYYY-MM-DD)
        - amount_per_10: 每 10 股派息（元，A 股惯例）
        - amount_per_share: 每股派息（元）
        - ex_date: 除权除息日（持有到此日开盘前才能拿到）
        - record_date: 股权登记日
        - status: 「实施 / 预案 / 决议公告」（**预案**未必最终落地）
        - has_split: 是否含送股 / 转增
    - ttm: 滚动 12 月分红汇总：
        - total_per_share: 累计每股分红（元，**仅累计 status=实施 的事件**）
        - events: 12 月内已落地分红次数（A 股年报+中报通常 1-2 次）
        - yield_pct: 年化股息率 = ttm.total_per_share / 现价 * 100（best-effort）
        - window_days: 365
    - source: akshare/stock_history_dividend_detail（数据源新浪）

    限制 / 注意事项：
    - **当前只支持 A 股**（包括沪深主板 / 创业板 / 科创板 / 北交所），HK/US 暂未实现
    - 「预案」状态的分红还没真发——agent 引用时要明确区分
    - akshare 拉取偶尔超时，失败会返回空 history（不会抛错）
    """
    return _get_dividend_history(query, last_n=last_n)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "starting BigV-twins MARKET MCP server on %s:%d (streamable-http)",
        settings.mcp_host, settings.mcp_market_port,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
