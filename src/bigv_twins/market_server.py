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
        "    - `get_dividend_history(query)` — A 股分红历史 + 历史股息率(算法1) + 预测股息率(算法2)\n"
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
    """获取 A 股分红 + **两个股息率算法**（历史口径 + 预测口径）。

    用户问到「X 的分红 / 股息率 / 历年派息 / 当下买入收益率」时**必先调这个**。
    `get_stock_snapshot` 只给 PE/PB/市值，不含分红——分红逻辑独立。

    ## 返回结构

    - `current_price`: 当前股价（两个算法共用）
    - `history[]`: 最近 last_n 条分红事件（公告日期/派息/除权日/股权登记日/进度）
    - `by_fiscal_year[]`: **按财年分组**（最新在前）
        - `fy`, `h1_div_per_10`, `fy_div_per_10`, `total_div_per_share`
        - `eps_fy`: 该 FY 的每股收益（从年报 12-31 行拿）
        - `payout_ratio`: 派息率 = total_div_per_share / eps_fy
        - `is_announced`: 年报分红是否至少决议通过
    - `algorithm_1_historical`: **历史口径股息率**（详见下方）
    - `algorithm_2_forecast`: **预测口径股息率**（详见下方）
    - `ttm`: legacy 字段（滚动 12 月），**优先使用 algorithm_1**

    ## 算法 1（历史口径）

    选最近一个已公告完成的财年（年报至少经股东大会决议通过），其中报 + 年报
    所有现金分红 / 当前股价 = 历史股息率。代表「**按上一财年实际派息算的当前
    买入收益率**」——这是大多数投资者口里"股息率"的标准定义。

    ## 算法 2（预测口径）

    取近 3 年（最少 2 年）的派息率均值 × 用 EPS 增长率外推得到的下一年预测 EPS
    / 当前股价 = 预测股息率。

    步骤（output 的 `calculation` 字段会展示完整推导）：
      1. 各年派息率 = 当年分红/股 ÷ 当年 EPS_FY
      2. 平均派息率
      3. 各年 EPS YoY 增长率
      4. 平均增长率
      5. 预测下一年 EPS = 最新 EPS × (1 + 平均增长率)
      6. 预测下一年分红 = 预测 EPS × 平均派息率
      7. 算法 2 股息率 = 预测分红 / 当前股价

    **预测值仅供参考**——一次性损益（如保险公司投资收益异常年份）会让 EPS
    某年突增/突跌，平均增长率被拉偏，外推就失真。output 的 `note` 字段会
    flag 这种异常（任一年 EPS 波动 > 40% 时）。

    ## ⚠ Agent 必须遵守的展示规则

    1. **两个算法都要展示**——不要只挑其中一个，用户要看到两面信息
    2. **展示完整 `calculation` 字段**——让用户看清每一步推导
    3. **明确标注**：算法 1 = 历史口径；算法 2 = **预测仅供参考**
    4. 如果 `algorithm_2_forecast.note` 有内容（一次性损益警告），**重点提示用户**
    5. 如果你是博主分身且有自己的派息率 / 估值方法论：
       - **必须先调用此工具**并展示工具的两个算法结果
       - 然后再加你自己的解读（"按我的方法看……")，但要标清楚哪是工具计算、哪是你的判断

    ## 限制

    - 支持 A 股（沪深主板 / 创业板 / 科创板 / 北交所）和港股（HK）
    - 港股只有算法 1（历史口径），无算法 2（因 akshare HK EPS 数据不全）
    - US 暂未实现
    - 「预案」分红还没真发，算法 1 只取 "实施" 或 "决议通过" 的年报
    - 算法 2 需至少 2 个完整财年数据；不够会返回 note 而非数字
    - akshare 拉取偶尔超时——失败会返回空 history（不抛错）
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
