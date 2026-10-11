import os

from tavily import TavilyClient

from tools.Toolregistry import tool


@tool(name="web_search", description="联网搜索最新信息，返回相关网页的标题、链接和摘要，用于查询实时新闻、最新数据等")
def web_search(query, max_results=5):
    client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    result = client.search(
        query=query,
        search_depth="basic",
        max_results=max_results,
        include_answer=True,
    )

    lines = []
    answer = result.get("answer")
    if answer:
        lines.append(f"摘要：{answer}")

    for i, item in enumerate(result.get("results", []), 1):
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")

    if not lines:
        return "未找到相关结果"

    return "\n\n".join(lines)
