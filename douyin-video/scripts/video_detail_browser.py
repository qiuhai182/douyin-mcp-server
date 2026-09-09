#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser-based fallback for single-video info parsing.

Douyin risk-controls the plain-HTTP share page (videoInfoRes replaced by an
empty shell). Opening the desktop video page in a real browser and capturing
the aweme/detail XHR works reliably, so we use it as a fallback channel.
"""

import re
from typing import Optional


def fetch_video_info_via_browser(video_id: str, timeout_ms: int = 60000) -> dict:
    """Get {url, title, author, video_id} for one video via headless browser.

    Returns the same dict shape as DouyinProcessor.parse_share_url.
    Raises RuntimeError if nothing could be captured.
    """
    from playwright.sync_api import sync_playwright

    found = {}

    with sync_playwright() as p:
        browser = None
        last_err = None
        for channel in ("msedge", "chrome"):
            try:
                browser = p.chromium.launch(channel=channel, headless=True)
                break
            except Exception as e:
                last_err = e
        if browser is None:
            raise RuntimeError(f"No Edge/Chrome available for browser fallback: {last_err}")

        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1380, "height": 900},
                locale="zh-CN",
            )
            page = context.new_page()

            def _on_response(resp):
                try:
                    u = resp.url
                    if "aweme/detail" not in u and "aweme/post" not in u:
                        return
                    if resp.status != 200:
                        return
                    data = resp.json()
                except Exception:
                    return

                details = []
                detail = data.get("aweme_detail")
                if detail:
                    details.append(detail)
                inner = (data.get("data") or {})
                if isinstance(inner, dict):
                    if inner.get("aweme_detail"):
                        details.append(inner["aweme_detail"])
                    for item in inner.get("aweme_list") or []:
                        details.append(item)
                for item in data.get("aweme_list") or []:
                    details.append(item)
                for item in data.get("item_list") or []:
                    details.append(item)

                for detail in details:
                    if not isinstance(detail, dict):
                        continue
                    aweme_id = str(detail.get("aweme_id") or "")
                    if not aweme_id:
                        continue
                    play = (detail.get("video") or {}).get("play_addr") or {}
                    url_list = play.get("url_list") or []
                    entry = {
                        "url": url_list[0] if url_list else "",
                        "title": (detail.get("desc") or "").strip(),
                        "author": ((detail.get("author") or {}).get("nickname") or "").strip(),
                    }
                    if aweme_id == str(video_id):
                        found[aweme_id] = entry
                    else:
                        found.setdefault(aweme_id, entry)

            page.on("response", _on_response)
            page.goto(
                f"https://www.douyin.com/video/{video_id}",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(8000)
        finally:
            browser.close()

    if str(video_id) in found:
        info = found[str(video_id)]
        if info.get("url"):
            return {"url": info["url"], "title": info["title"],
                    "author": info.get("author", ""), "video_id": str(video_id)}

    # detail XHR may not fire if page loaded from cache; try slug from URL state
    raise RuntimeError(
        f"Browser fallback could not capture video detail for {video_id}. "
        "The page may require login or the video may be private/removed."
    )
