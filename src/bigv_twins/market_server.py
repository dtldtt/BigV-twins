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
from .stock_data import get_stock_snapshot as _get_stock_snapshot

log = logging.getLogger("bigv_twins.market_server")

mcp = FastMCP(
    "bigv-market",
    instructions=(
        "Real-time market data tools. `get_stock_snapshot(query)` fetches "
        "current valuation / market cap / ownership / sector / index context "
        "for a specific stock (A-share / HK / US). `get_market_context(topics)` "
        "fetches recent (1w + 1m) performance for macro topics like 港股 / 黄金 / "
        "煤炭 / AI. Both are read-only, no LLM, no auth needed (loopback only)."
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
