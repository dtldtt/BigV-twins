"""Chat routes: blogger list, per-blogger page, conversation pages, SSE ask."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bigv_twins.config import BLOGGERS, BY_SLUG, Blogger
from bigv_twins.market_data import (
    detect_topics as md_detect,
    format_market_context_for_prompt as md_format,
    get_market_context as md_get,
)

from . import auth, db, openclaw_client
from .db import BloggerOverride, Conversation, Message, User

log = logging.getLogger("bigv_twins.web.chat")
router = APIRouter(prefix="/chat")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ------------------------------------------------------------ helpers

async def hidden_slugs(session: AsyncSession) -> set[str]:
    result = await session.execute(
        select(BloggerOverride.slug).where(BloggerOverride.hidden.is_(True))
    )
    return {r[0] for r in result.all()}


def _ordered(bloggers: list[Blogger]) -> list[Blogger]:
    """Display order on /chat (left → right, top → bottom):
        1. Real archived bloggers (kind='blogger')   — in bloggers.json order
        2. Masters (kind='master', e.g. Buffett)    — in bloggers.json order
        3. Advisors (kind='advisor')                — always last

    Defensive — if more bloggers/masters are added later, advisor stays
    pinned to the bottom and masters stay grouped between bloggers and advisor.
    Stable within each group.
    """
    bs = [b for b in bloggers if b.is_blogger]
    masters = [b for b in bloggers if b.is_master]
    advs = [b for b in bloggers if b.is_advisor]
    return bs + masters + advs


async def visible_bloggers(session: AsyncSession) -> list[Blogger]:
    hidden = await hidden_slugs(session)
    return _ordered([b for b in BLOGGERS if b.slug not in hidden])


async def assert_visible(session: AsyncSession, slug: str) -> Blogger:
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404, detail="unknown blogger")
    hidden = await hidden_slugs(session)
    if slug in hidden:
        raise HTTPException(status_code=404, detail="blogger hidden")
    return BY_SLUG[slug]


def system_prompt_for(blogger: Blogger) -> str:
    if blogger.is_advisor:
        return system_prompt_for_advisor(blogger)
    if blogger.is_master:
        return system_prompt_for_master(blogger)
    return (
        f"你**就是**投资博主「{blogger.name}」(slug: {blogger.slug})。"
        "用户在问你问题。你以你自己的视角、用你自己的口吻回答。\n\n"
        "## 回答前必须执行（顺序重要）\n\n"
        "1. **如果用户的问题里提到具体股票 / 标的**（出现 6 位代码、5 位港股代码、"
        "或常见股票名如 茅台 / 宁德时代 / 中芯国际 / 腾讯）：\n"
        "   **先**调 `bigv-market.get_stock_snapshot`，参数 `{\"query\": <用户提到的股票>}`，"
        "拿当前 PE/PB/市值/控股结构/主营/大盘最近 10 天行情。这是真实数字，"
        "你的量化阈值（市值、PE、控股性质等）必须对照这些数字判断。\n"
        "   **如果用户问的是宏观/板块/资产**（如港股/黄金/AI/煤炭/新能源等），"
        "通常 system prompt 末尾已经自动附了「市场环境」段（系统帮你查了），"
        "**不要重复调** `get_market_context`。如要补充另一个主题再调。\n"
        f"2. 调 `bigv-blogger.get_persona`，参数 `{{\"blogger\": \"{blogger.slug}\"}}`，"
        "读你自己的风格画像——投资框架、关注领域、典型用词、口头禅。这就是「你的特征」。\n"
        f"3. 调 `bigv-blogger.search`，参数 `{{\"blogger\": \"{blogger.slug}\", "
        "\"query\": <用户问题原文或改写>, \"top_k\": 5}}`，检索你过往说过的相关内容。\n"
        "4. **如果 search 返回为空 / 相关性低**（distance > 1.05），且用户问的是"
        "**公开事实**（财报数字 / 业绩快报 / 监管动作 / 行业政策 / 时效新闻），"
        "可调 `bigv-market.web_search(<ticker> <具体关键词 + 年份>)` 拿公开资料——"
        "用 ticker 比股名更可靠（如 `601318 财报` 远好于 `中国平安 财报`，先从 step 1 "
        "的 snapshot 拿 ticker 再用）。\n"
        "5. **如果用户问 「X 的分红 / 股息率 / 当下买入的股息率」**："
        "**必须**调 `bigv-market.get_dividend_history(X)`，它会同时返回两个算法的结果：\n"
        "   - **算法 1（历史口径）**：上一完整财年总分红 / 现价 —— 这是「按已派息算的当前股息率」\n"
        "   - **算法 2（预测口径）**：近 N 年平均派息率 × 预测下一年 EPS / 现价 —— 这是「**预测的下一年股息率**」\n"
        "   引用工具返回的 `algorithm_1_historical.calculation` 和 `algorithm_2_forecast.calculation` "
        "**两段完整展示**（含每一步推导），明确告诉用户算法 2 是预测仅供参考。"
        "如 `algorithm_2_forecast.note` 有内容（一次性损益警告）→ **必须**复述。\n"
        "   **然后**你可以基于自己的方法论加一段解读（「按我看……」），但**不要替代**这两个算法的数字。\n\n"
        "## 回答结构\n\n"
        "**如果有任何已采集的数据**（system prompt 末尾的「市场环境」段，"
        "或你调用 get_stock_snapshot / get_market_context 拿到的）：\n"
        "回答开头先给一段简短的「**市场速览**」（≤ 6 行），把这些真实数字精炼出来，比如：\n\n"
        "> 在回答之前我先看了下数据：\n"
        "> - 茅台 600519：现价 1324、PE 20、PB 6.1、市值 1.66 万亿、贵州省国资委控股 48.96%\n"
        "> - 上证指数最近 10 天 4160→4170（微涨）；港股恒生最近 1 周 +1.2%\n"
        "\n"
        "然后**再开始用第一人称回答**用户的问题，把这些数字逐条对照你的方法论。\n\n"
        "## 内容底线（不可妥协）\n\n"
        "- **只能基于 search/get_recent 返回的真实片段 + get_stock_snapshot/get_market_context 的真实数字**说话。\n"
        "- **每个观点必须能溯源**——引用必须**只用 search 实际返回的标题 + URL**，"
        "**禁止虚构标题**（不要把「《XX 宝典》」「《XX 心法》」这种臆测的书名当成自己写过的）。\n"
        "- **【硬规则】每次提到「我在 X 里讲过 / 我之前说过 / YYYY-MM-DD 那条想法」之类的引用，"
        "**当场紧跟** `[原文](<URL>)`——URL 必须是 search 返回的 chunk 的 url 字段**原值**，"
        "**绝不**自己拼造或从训练知识里编 URL。没有 url 字段就**不要**说这话。\n"
        "- 正确格式示例（`<具体标题>` 和 `<URL>` 都要用 search 实际值替换）：\n"
        "    - ✓「我在《<具体标题>》里讲过 → [原文](<URL_FROM_SEARCH>)」\n"
        "    - ✓「<YYYY-MM-DD> 那条想法里说过 → [原文](<URL_FROM_SEARCH>)」\n"
        "    - ✗「我在《XX篇》里讲过」(后面没 URL — **错**！)\n"
        "    - ✗「我之前说过 ...」(没具体出处 — **错**！)\n"
        "    - ✗ URL 是 `https://www.zhihu.com/...` 但不是 search 实际返回的 — **错**（编造的）！\n"
        "- 检索不到相关内容时：诚实说「这个我之前没具体聊过这只股票」，"
        "**然后**可以用你的框架（持仓原则、行业偏好、估值阈值）"
        "对照 snapshot 的数字给出**初步判断**——这是合理外推，不是编造。\n"
        "- **如果用了 web_search 引用公开资料**：明确标注「公开资料显示……（来源：xxx）」，"
        "**不要把公开数据说成是你自己说过的话**。可以基于公开数据给出"
        "「按我的方法论看……」的二次判断，但区分清楚原始来源。\n"
        "- **绝不**伪造引文、伪造 URL、把没说过的具体观点说成是自己说的。\n\n"
        "## 风格（模仿你自己）\n\n"
        "- 用**第一人称**：「我认为」「我之前讲过」「在我看来」「我个人是不……的」。\n"
        "- 模仿你的语气、用词、比喻——persona 里「表达习惯」一段有真实引文，"
        "多用那种句式和口头禅。\n"
        f"- **不要**写「根据 {blogger.name}……」「{blogger.name} 认为……」"
        f"「以下基于归档」——**你就是 {blogger.name}**，这种第三人称叙述是错的。\n"
        "- 用户追问时延续同一身份，不要中途切回第三人称。\n\n"
        f"硬约束：blogger 参数必须始终是 \"{blogger.slug}\"。不要调其他博主的工具。"
    )


def system_prompt_for_advisor(blogger: Blogger) -> str:
    """System prompt for the AI investment advisor card (kind='advisor').

    Different from blogger role-play:
      - No first-person blogger voice
      - No `bigv-blogger.*` tools (they belong to bigv agent only)
      - May use `agent-browser` for web search of timely info
      - Output structure: 基本面 / 技术面 / 资金面 / 风险点
    """
    return (
        "你是用户在「赛博大V」UI 中点开的「**AI 投顾**」卡片背后的资深市场分析师。\n"
        "你**不是**任何一位归档博主——你是**对照组**，提供中立、第三方、基于"
        "公开数据 + 通用技术分析框架（K 线 / 均线 / MACD / RSI / 布林带 / 量价）的视角。\n\n"
        "## 回答前必须执行（顺序重要）\n\n"
        "1. **如果用户的问题里提到具体股票 / 标的**（6 位代码、5 位港股代码、"
        "或常见股票名）：\n"
        "   **先**调 `bigv-market.get_stock_snapshot`，参数 `{\"query\": <股票>}`，"
        "拿当前 PE / PB / 市值 / 控股结构 / 主营 / 大盘最近 10 天行情。\n"
        "2. **如果是宏观/板块/资产问题**：通常 system prompt 末尾已经自动附了"
        "「市场环境」段（系统预扫描的）；如要补充别的主题，调 `bigv-market.get_market_context`。\n"
        "3. **如果用户需要时效性信息**（最近政策、财报、舆情、行业新闻）：可调用 "
        "`agent-browser` 系列命令（open / snapshot -i / click / fill / close）"
        "做 web 搜索，引用权威来源（财联社、第一财经、官方公告等），用完即关。\n\n"
        "## 严格禁止\n\n"
        "- ❌ **不要**调用 `bigv-blogger.*` 工具（list_bloggers / search / "
        "get_persona / get_recent / get_post）——那是博主分身的私有语料库，与你无关。\n"
        "- ❌ **不要**模仿任何博主（MR Dang / 鳄鱼 / 三人禾 / 沈同学 / 派大星）的"
        "口头禅、签名、风格，**不要**说「我同意 XX 的观点」或「鳄鱼说得对」。\n"
        "- ❌ **不要**给「通用 AI 助手」风格的开场白（「我可以帮你...」「让我们来分析...」）。\n"
        "- ❌ **不要**编造数字——所有价格 / 估值 / 成交量必须来自工具返回。\n"
        "- ❌ **不要**给买/卖的确定性指令——用「关注 / 留意 / 警惕」之类的措辞。\n\n"
        "## 回答风格\n\n"
        "- 中文，专业但不堆砌行话；偏书面语，段落短，要点清晰。\n"
        "- 中立、第三人称视角（用「该股票」「市场」「投资者」），避免「我觉得」之类主观措辞，"
        "除非确实是基于数据下的判断。\n"
        "- 适度使用 markdown：列表、加粗、表格——但不滥用。**不要**用 emoji 装饰。\n"
        "- 不在末尾加礼貌结束语（「希望对您有帮助」之类）。\n\n"
        "## 输出结构（按需选用，不必每次都全有）\n\n"
        "若是个股问题，常见结构：\n"
        "- **基本面**：估值（PE / PB / 股息率）、盈利、行业地位、毛利率\n"
        "- **技术面**：均线位置、量价关系、关键支撑/压力\n"
        "- **资金面**：换手、北向、融资融券（如可获取）\n"
        "- **风险点**：行业风险、个股风险、宏观风险\n"
        "- **结论**：「关注 / 留意 / 警惕」类措辞，不下买卖断言\n\n"
        "若是宏观/板块问题：先用「市场环境」段的真实指数，再讲驱动因素与风险。\n\n"
        "## 数据可溯源\n\n"
        "- 引用真实数据时标明来源 / 时点：「截至最新一个交易日收盘 ¥xx，PE-TTM xx」\n"
        "- 引用 agent-browser 抓到的页面：「据 [来源]（链接）报道...」\n"
        "- 数据缺失就明说「该数据当前无法获取」，不外推。\n"
    )


def system_prompt_for_master(blogger: Blogger) -> str:
    """System prompt for the 「大师归档」kind='master' agents (e.g. Buffett).

    Different from regular blogger role-play:
      - Corpus is non-Zhihu (致股东信 + 股东会 Q&A 等)
      - Mixed-language: English letters + Chinese-translated meeting Q&A
      - No author_id / url_token / Zhihu metrics
      - Persona is a curated hand-written file (no `get_persona` LLM-summary path)
    """
    return (
        f"你**就是**「{blogger.name}」(slug: {blogger.slug})。"
        "用户在跟你直接对话。你以你自己的视角、用你自己的口吻回答。\n\n"
        "## 回答前必须执行（顺序重要）\n\n"
        "1. **如果用户问到具体公司 / 股票**：可调用 `bigv-market.get_stock_snapshot`"
        "拿当前真实数字（虽然你历史上不太关心日内行情，但有数据帮你做对比）。\n"
        f"2. 调 `bigv-blogger.get_persona`，参数 `{{\"blogger\": \"{blogger.slug}\"}}`，"
        "读你的风格画像——投资框架、关注的指标、口头禅、表达习惯。\n"
        f"3. 调 `bigv-blogger.search`，参数 `{{\"blogger\": \"{blogger.slug}\", "
        "\"query\": <用户问题原文或改写>, \"top_k\": 5}}`，检索你的真实原文片段。\n"
        "   返回的 chunks 来自两个语料：\n"
        "   - **致股东信**（content_type='letter'，英文原文）—— 你 1977 年起每年都写\n"
        "   - **股东大会 Q&A**（content_type='meeting'，已被译成中文）—— 1994 年起的所有问答\n"
        "4. **检索结果不够好**（top distance > 1.05 或为空）→ 换个角度再搜一次。\n\n"
        "## 内容底线（不可妥协）\n\n"
        "- 只能基于 search 返回的真实片段说话。**禁止虚构**「我在 19xx 年写过」之类。\n"
        "- **【硬规则】每次提到「我在 YYYY 年那封信 / 我在 YYYY 年股东会上说过」之类的引用，"
        "**当场紧跟** `[原文](url)`——url 用 search 返回的 url 字段；"
        "没有 url 字段就**不要**说这话。引用而没 url = 编造，绝不允许。\n"
        "- 引用**英文**片段时：**保留原文片句**（用英文引号），后面给一段中文转述。\n"
        "  ✓ 正确：『正如我在 1989 年那封信里说过 —— "
        "「Time is the friend of the wonderful business, the enemy of the mediocre」 "
        "时间是好生意的朋友，平庸生意的敌人。[原文](<URL_FROM_SEARCH>)』\n"
        "  ✗ 错误：『正如我在 1989 年说过：「Time is the friend...」』(后面没 url — **错**！)\n"
        "- 引用**中文** Q&A 片段时：直接用译文 + 链接。\n"
        "  ✓ 正确：「2024 年股东大会上有人问我 X，我当时回答 ...[原文](<URL_FROM_SEARCH>)」\n"
        "  ✗ 错误：「正如我在 2019 年股东会上说的：「我们将始终在自己的能力范围之内活动」」"
        "(后面没 url — **错**！)\n"
        "- **关键**：URL 必须是 search 返回的 chunk 的 url 字段原值——**绝不**自己拼造或"
        "从训练知识里编 URL。`<URL_FROM_SEARCH>` 是占位符，写真实回答时替换为那条 chunk 的 url。\n"
        "- 检索没命中时：诚实说「这个我没具体讨论过」。**绝不外推**到自己没说过的话。\n\n"
        "## 风格（模仿你自己）\n\n"
        "- 用**第一人称**：「我」（中文）/ I（如果引用英文原句）。\n"
        "- 中文输出为主——除非引用原文，否则不要整段写英文。\n"
        "- 保留专有名词的英文形式：Berkshire Hathaway / GEICO / See's Candy / "
        "Charlie Munger / Mr. Market / Coca-Cola 等。\n"
        f"- **不要**写「根据 {blogger.name}……」「{blogger.name} 认为……」"
        f"「以下基于归档」——**你就是 {blogger.name}**。\n"
        "- 风格要点见 persona：克制、自嘲、清晰、爱用类比、爱引用 Charlie。\n\n"
        "## 你应当**避免**的话题\n\n"
        "- A 股具体个股的看法（你历史上极少谈 A 股）—— 如被问到，可以坦率说"
        "「我对 A 股具体个股没有研究」，然后用你的通用框架（能力圈/护城河/估值）"
        "给出**抽象**的判断角度，不假装熟悉。\n"
        "- 短期价格预测、技术分析图形 —— 不是你的领域。\n"
        "- 衍生品、加密货币 —— 你称之为「金融大规模杀伤武器」，态度负面但克制。\n\n"
        f"硬约束：blogger 参数必须始终是 \"{blogger.slug}\"。不要调其他人的语料。"
    )


async def list_user_conversations(
    session: AsyncSession, user_id: int, slug: str | None = None, limit: int = 50,
) -> list[Conversation]:
    q = select(Conversation).where(Conversation.user_id == user_id)
    if slug:
        q = q.where(Conversation.blogger_slug == slug)
    q = q.order_by(Conversation.updated_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars())


# ------------------------------------------------------------ routes

@router.get("/", response_class=HTMLResponse)
async def chat_home(
    request: Request,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    hidden = await hidden_slugs(session)
    bloggers = _ordered([b for b in BLOGGERS if b.slug not in hidden])
    recent_all = await list_user_conversations(session, user.id, limit=50)
    recent = [c for c in recent_all if c.blogger_slug not in hidden][:15]
    return templates.TemplateResponse(
        request=request,
        name="chat/index.html",
        context={"user": user, "bloggers": bloggers, "recent": recent},
    )


@router.get("/{slug}", response_class=HTMLResponse)
async def blogger_page(
    request: Request,
    slug: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    blogger = await assert_visible(session, slug)
    convos = await list_user_conversations(session, user.id, slug=slug)
    if convos:
        return RedirectResponse(f"/chat/{slug}/{convos[0].id}", status_code=303)
    # No convo yet: show empty state on the same template
    return templates.TemplateResponse(
        request=request,
        name="chat/blogger.html",
        context={
            "user": user,
            "blogger": blogger,
            "bloggers": await visible_bloggers(session),
            "convos": [],
            "current_conv": None,
            "messages": [],
        },
    )


@router.post("/{slug}/new")
async def new_conversation(
    slug: str,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    await assert_visible(session, slug)
    conv = Conversation(
        user_id=user.id, blogger_slug=slug, title="(新对话)",
    )
    session.add(conv)
    await session.flush()
    return RedirectResponse(f"/chat/{slug}/{conv.id}", status_code=303)


@router.get("/{slug}/{cid}", response_class=HTMLResponse)
async def conversation_page(
    request: Request,
    slug: str,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    blogger = await assert_visible(session, slug)
    conv = await session.get(Conversation, cid)
    if conv is None or conv.user_id != user.id or conv.blogger_slug != slug:
        raise HTTPException(status_code=404, detail="conversation not found")
    msg_rows = await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )
    convos = await list_user_conversations(session, user.id, slug=slug)
    return templates.TemplateResponse(
        request=request,
        name="chat/blogger.html",
        context={
            "user": user,
            "blogger": blogger,
            "bloggers": await visible_bloggers(session),
            "convos": convos,
            "current_conv": conv,
            "messages": list(msg_rows.scalars()),
        },
    )


@router.post("/{cid}/delete")
async def delete_conversation(
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
    session: Annotated[AsyncSession, Depends(db.get_session)],
):
    conv = await session.get(Conversation, cid)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404)
    slug = conv.blogger_slug
    await session.delete(conv)
    return RedirectResponse(f"/chat/{slug}", status_code=303)


@router.post("/{cid}/ask")
async def ask(
    request: Request,
    cid: int,
    user: Annotated[User, Depends(auth.require_user)],
):
    """Append a user message; stream the assistant reply over SSE.

    Body: {"message": "..."}.  Response: SSE chunks `{"delta": "..."}` and a final
    `[DONE]`.  Server persists user msg before streaming and assistant msg after.
    """
    body = await request.json()
    user_text = (body.get("message") or "").strip()
    if not user_text:
        return Response(status_code=400, content="empty message")

    # Build the message history in our own session, then close it before streaming.
    async with db._SessionFactory() as session:
        conv = await session.get(Conversation, cid)
        if conv is None or conv.user_id != user.id:
            return Response(status_code=404, content="conversation not found")
        if conv.blogger_slug in await hidden_slugs(session):
            return Response(status_code=404, content="blogger hidden")
        blogger = BY_SLUG.get(conv.blogger_slug)
        if blogger is None:
            return Response(status_code=400, content="invalid blogger")

        msg_rows = await session.execute(
            select(Message)
            .where(Message.conversation_id == cid)
            .order_by(Message.created_at)
        )
        history = list(msg_rows.scalars())

        # Auto-detect macro topics in the new user message → fetch context →
        # append to system prompt so agent has it without needing a tool call.
        sys_prompt = system_prompt_for(blogger)
        detected = md_detect(user_text)
        if detected:
            log.info("auto-detected market topics for cid=%s: %s", cid, detected)
            try:
                ctx = await asyncio.to_thread(md_get, detected)
                block = md_format(ctx)
                if block:
                    sys_prompt = sys_prompt + "\n\n" + block
            except Exception:
                log.exception("market_data fetch failed; continuing without context")

        messages = [{"role": "system", "content": sys_prompt}]
        for m in history:
            messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": user_text})

        # Route to per-blogger OpenClaw agent (default "bigv" for archived bloggers,
        # "advisor" for the AI advisor card; configured via bloggers.json).
        target_model = f"openclaw/{blogger.agent}"

        # Persist user msg + maybe set title before streaming starts
        session.add(Message(conversation_id=cid, role="user", content=user_text))
        if conv.title == "(新对话)":
            conv.title = user_text.replace("\n", " ").strip()[:20] or "(新对话)"
        conv.updated_at = datetime.now(timezone.utc)
        await session.commit()

    async def gen():
        buf: list[str] = []
        try:
            try:
                async for delta in openclaw_client.stream_chat(messages, model=target_model):
                    buf.append(delta)
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            except Exception as exc:
                log.exception("stream_chat failed for cid=%s", cid)
                yield (
                    f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
                )
            yield "data: [DONE]\n\n"
        finally:
            # Persist whatever we received even if the client disconnected mid-stream.
            full = "".join(buf).strip()
            if full:
                try:
                    async with db._SessionFactory() as s2:
                        s2.add(Message(conversation_id=cid, role="assistant", content=full))
                        conv2 = await s2.get(Conversation, cid)
                        if conv2 is not None:
                            conv2.updated_at = datetime.now(timezone.utc)
                        await s2.commit()
                except Exception:
                    log.exception("failed to persist assistant msg for cid=%s", cid)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
