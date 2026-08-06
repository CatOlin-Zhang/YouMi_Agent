"""
Web 操作工具

提供网络相关工具:
- web_fetch: 抓取网页内容（纯文本提取）

使用 httpx（项目已有依赖）进行 HTTP 请求。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 简单 HTML 标签去除（不引入额外依赖）
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\n\s*\n")


async def web_fetch(
    url: str,
    timeout: int = 15,
    max_chars: int = 8000,
    headers: str = "",
) -> str:
    """抓取网页内容并提取纯文本。

    Args:
        url: 目标网页 URL（http 或 https）
        timeout: 请求超时秒数，默认 15
        max_chars: 最大返回字符数，默认 8000
        headers: 额外的 HTTP 请求头（JSON 字符串），可选
    """
    import json as _json

    if not url.startswith(("http://", "https://")):
        return f"错误: URL 必须以 http:// 或 https:// 开头"

    logger.info("web_fetch: url=%s timeout=%d", url, timeout)

    try:
        import httpx
    except ImportError:
        return "错误: httpx 未安装，无法执行 web_fetch"

    extra_headers: dict[str, str] = {}
    if headers:
        try:
            extra_headers = _json.loads(headers)
        except _json.JSONDecodeError:
            return "错误: headers 参数不是有效的 JSON 字符串"

    default_headers = {
        "User-Agent": "YouMi-Agent/0.1 (web_fetch tool)",
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    }
    default_headers.update(extra_headers)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=default_headers,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

    except httpx.TimeoutException:
        return f"错误: 请求超时（{timeout}s）- {url}"
    except httpx.HTTPStatusError as e:
        return f"错误: HTTP {e.response.status_code} - {url}"
    except Exception as e:
        return f"错误: 请求失败 - {e}"

    content_type = response.headers.get("content-type", "")

    # JSON 响应直接返回
    if "application/json" in content_type:
        text = response.text
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (已截断，共 {len(text)} 字符)"
        return f"[URL: {url} | Content-Type: application/json]\n{text}"

    # HTML 提取纯文本
    html = response.text
    text = _extract_text(html)

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (已截断，共 {len(text)} 字符)"

    logger.info("web_fetch: %s → %d chars", url, len(text))
    return f"[URL: {url}]\n{text}"


def _extract_text(html: str) -> str:
    """从 HTML 中提取纯文本"""
    # 去除 script 和 style
    text = _SCRIPT_RE.sub("", html)
    text = _STYLE_RE.sub("", text)
    # 去除标签
    text = _TAG_RE.sub(" ", text)
    # HTML 实体
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    # 清理多余空白
    text = _WHITESPACE_RE.sub("\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text
