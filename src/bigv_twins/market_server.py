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
from .web_search import web_search as _web_search

log = logging.getLogger("bigv_twins.market_server")

mcp = FastMCP(
    "bigv-market",
    instructions=(
        "Market data tools in three tiers — choose by question shape:\n"
        "  TIER 1 (structured, fast, cached, deterministic schema):\n"
        "    - `get_stock_snapshot(query)` — 估值 / 市值 / 控股 / 行业\n"
        "    - `get_dividend_history(query)` — A 股分红历史 + TTM 股息率\n"
        "    - `get_market_context(topics)` — 大盘 / 板块 / 主题最近 1w+1m 走势\n"
        "  TIER 2 (general web search, snippets only, ~1s):\n"
        "    - `web_search(query, mode)` — Bing CN，财经类自动加 site:filter\n"
        "  TIER 3 (deep page fetch, in `advisor` workspace only):\n"
        "    - `agent-browser` skill — headless 浏览器，慢但能拿完整内容\n"
        "Decision rule: try TIER 1 first. If not covered, TIER 2 (snippet 通常够答). "
        "If snippet 不够明确再 TIER 3 进具体 url。"
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


@mcp.tool()
def web_search(
    query: Annotated[
        str,
        Field(description="搜索关键词。**强烈建议带股票代码或具体年份**，详见 docstring"),
    ],
    top_k: Annotated[int, Field(ge=1, le=10)] = 5,
) -> dict:
    """通用 web 搜索（Bing CN，**Tier 2 工具，结构化 MCP 不覆盖时的兜底**）。

    返回 top_k 条搜索结果 `{title, url, snippet, source}`。snippet 通常
    已经包含关键数字 / 事实，**不需要再点进去**就能答用户的问题。

    ## 何时用 web_search vs 其他工具

    | 用户问题 | 该用 |
    |---------|------|
    | 「X 股票现价 / 估值 / 市值 / 控股」 | `get_stock_snapshot` (Tier 1) |
    | 「X 分红 / 股息率 / 历年派息」 | `get_dividend_history` (Tier 1) |
    | 「X 板块 / 大盘走势」 | `get_market_context` (Tier 1) |
    | 「X 财报 / 业绩 / 营收」 | **web_search** (没结构化工具) |
    | 「X 最新公告 / 业绩快报 / 新闻」 | **web_search** |
    | 「行业政策 / 产业链分析 / 研报观点」 | **web_search** |
    | snippet 还不够明确（advisor 专属） | `agent-browser` 进 top URL (Tier 3) |

    ## ⚠ Query 构造规则（很重要 —— Bing CN 对短查询很挑）

    Bing CN 会对泛指查询误判，把「中国 平安」拆成「中国」+「平安」然后返回
    中国政府网 / 人民网这种垃圾。本工具内置 junk-domain post-filter 会丢掉
    这些，但**如果 query 本身太宽，filter 完就空了**。

    避坑姿势：
    - ✓ **用股票代码替代股名**：`601318 财报` 远好于 `中国平安 财报`
      （先调 `get_stock_snapshot(中国平安)` 拿 ticker 再来搜）
    - ✓ **年份紧贴指标，不要空格**：`2025年报` ✓ / `2025 年报` ✗
    - ✓ **加具体术语**：`半年报 / 中报 / 年度报告 / 三季报`
    - ✗ 别用泛指：「X 业绩」「X 财报」单独用太宽
    - ✗ 别加双引号或 `site:` —— Bing CN 对这些无效

    ## 怎么使用返回结果

    1. **优先看 snippet**——大多数财经事实 snippet 里已有
    2. **引用必带 url**——读者要能核实
    3. **多源交叉**——同一数字两个来源吻合才说"市场普遍认为"
    4. **note 字段存在时**：说明 query 不够具体，**换词重搜一次**（用 ticker / 年份），
       仍空 → 诚实告诉用户「该信息无法可靠获取」

    硬限制：每个 user turn ≤ 3 次 web_search 调用（含 retry）。process 内缓存 10min。
    """
    return _web_search(query, top_k=top_k)


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
