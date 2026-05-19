"""Smoke client: connect to the local BigV-twins MCP server and exercise each tool."""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def parse_result(r):
    """FastMCP can return data as `structuredContent` (preferred) or as one/many text blocks."""
    sc = getattr(r, "structuredContent", None)
    if sc is not None:
        # FastMCP wraps a list under {'result': [...]} when the function returns a list,
        # and the dict itself when it returns a dict. Unwrap if there's a single 'result' key.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    items = []
    for block in r.content or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            items.append(json.loads(text))
        except json.JSONDecodeError:
            items.append(text)
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return items


async def main(url: str) -> None:
    async with streamablehttp_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== tools/list ===")
            tools = await session.list_tools()
            for t in tools.tools:
                desc = (t.description or "").strip().splitlines()[0] if t.description else ""
                print(f"  - {t.name}: {desc[:90]}")

            print("\n=== resources/list ===")
            try:
                templates = await session.list_resource_templates()
                for t in templates.resourceTemplates:
                    print(f"  - template {t.uriTemplate}")
            except Exception as e:
                print(f"  (template list error: {e})")

            print("\n=== call list_bloggers ===")
            r = await session.call_tool("list_bloggers", {})
            data = parse_result(r)
            if isinstance(data, list):
                for b in data:
                    print(
                        f"  {b['slug']:8s} name={b['name']!r:24s} has_persona={b['has_persona']} "
                        f"twin_db_exists={b['twin_db_exists']}"
                    )
            else:
                print(f"  unexpected: {type(data).__name__}: {data!r}")

            print("\n=== call search(eyu, '怎么看待A股市场', top_k=2) ===")
            r = await session.call_tool(
                "search", {"blogger": "eyu", "query": "怎么看待A股市场", "top_k": 2}
            )
            data = parse_result(r)
            if isinstance(data, list):
                for h in data:
                    print(
                        f"  dist={h['distance']:.4f} type={h['content_type']} "
                        f"voteup={h['voteup_count']}"
                    )
                    print(f"    text: {h['text'][:100]}…")
            else:
                print(f"  got {type(data).__name__}: {str(data)[:200]}")

            print("\n=== call get_recent(eyu, n=3) ===")
            r = await session.call_tool("get_recent", {"blogger": "eyu", "n": 3})
            data = parse_result(r)
            if isinstance(data, list):
                for p in data:
                    print(
                        f"  {p['created_time']} {p['content_type']:8s} "
                        f"voteup={p['voteup_count'] or 0:5d} title={p['title']!r}"
                    )
                first_zid = data[0]["zhihu_id"] if data else None
            else:
                print(f"  got: {data!r}")
                first_zid = None

            if first_zid:
                print(f"\n=== call get_post(eyu, zhihu_id={first_zid}) ===")
                r = await session.call_tool(
                    "get_post", {"blogger": "eyu", "zhihu_id": first_zid}
                )
                p = parse_result(r)
                if isinstance(p, dict):
                    print(
                        f"  type={p['content_type']} title={p['title']!r} "
                        f"text_len={len(p['text'])}"
                    )
                else:
                    print(f"  got: {p!r}")

            print("\n=== call get_persona(eyu) ===")
            r = await session.call_tool("get_persona", {"blogger": "eyu"})
            p = parse_result(r)
            if isinstance(p, dict):
                print(f"  available={p.get('available')}")
                print(f"  text: {p.get('text', '')[:140]}")
            else:
                print(f"  got: {p!r}")

            print("\n=== read resource persona://blogger/eyu ===")
            try:
                rr = await session.read_resource("persona://blogger/eyu")
                for c in rr.contents:
                    text = getattr(c, "text", None)
                    if text:
                        print(f"  {text[:140]}")
            except Exception as e:
                print(f"  (error: {e})")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770/mcp"
    asyncio.run(main(url))
