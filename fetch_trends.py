"""
Daily Dev Community Trends fetcher.

Sources:
  - Reddit r/programming (hot, JSON)
  - Stack Overflow (Stack Exchange API, hot)
  - GitHub Discussions (GraphQL search)

Pipeline: fetch -> score -> pick top N -> translate (DeepL -> Google) -> MD -> Slack.
Designed to be idempotent per day and resilient to single-source failures.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------- config ----------------
KST = timezone(timedelta(hours=9))
TODAY_KST = datetime.now(KST)
REPORT_DIR = Path("reports")
TOP_N = 5
PER_SOURCE_POOL = 3  # top-k per source before merging

DEEPL_KEY = os.getenv("DEEPL_API_KEY")
GH_TOKEN = os.getenv("GH_API_TOKEN")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")
SO_KEY = os.getenv("STACK_EXCHANGE_KEY")  # optional, raises daily quota

USER_AGENT = "dev-trends-bot/1.0 (by GitHub Actions)"
HEADERS_COMMON = {"User-Agent": USER_AGENT}

SOURCE_LABEL = {
    "reddit": "Reddit",
    "stackoverflow": "Stack Overflow",
    "github": "GitHub Discussions",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trends")


# ---------------- scoring ----------------
def unified_score(upvotes: int, comments: int, views: Optional[int] = None) -> float:
    """Log-scale weighted score. Comments weighted highest (signal > noise)."""
    s = math.log10(upvotes + 1) * 1.0 + math.log10(comments + 1) * 1.5
    if views and views > 0:
        s += math.log10(views + 1) * 0.3
    return s


# ---------------- sources ----------------
def fetch_reddit(limit: int = 25) -> list[dict]:
    url = "https://www.reddit.com/r/programming/hot.json"
    try:
        r = requests.get(url, headers=HEADERS_COMMON, params={"limit": limit}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("reddit fetch failed: %s", e)
        return []
    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        items.append({
            "source": "reddit",
            "title": d.get("title", ""),
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "upvotes": d.get("ups", 0) or 0,
            "comments": d.get("num_comments", 0) or 0,
            "views": d.get("view_count") or 0,
            "meta": f"r/programming · ↑{d.get('ups', 0)} · 💬{d.get('num_comments', 0)}",
        })
    return items


def fetch_stackoverflow(limit: int = 25) -> list[dict]:
    url = "https://api.stackexchange.com/2.3/questions"
    params = {
        "order": "desc",
        "sort": "hot",
        "site": "stackoverflow",
        "pagesize": limit,
    }
    if SO_KEY:
        params["key"] = SO_KEY
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("stackoverflow fetch failed: %s", e)
        return []
    items = []
    for q in data.get("items", []):
        items.append({
            "source": "stackoverflow",
            "title": q.get("title", ""),
            "url": q.get("link", ""),
            "upvotes": q.get("score", 0) or 0,
            "comments": q.get("answer_count", 0) or 0,
            "views": q.get("view_count", 0) or 0,
            "meta": (
                f"SO · ↑{q.get('score', 0)} · ✅{q.get('answer_count', 0)} "
                f"· 👀{q.get('view_count', 0)}"
            ),
        })
    return items


def fetch_github_discussions(limit: int = 25) -> list[dict]:
    """
    Public Discussions with high engagement via GraphQL search.
    Filter: updated in last 3 days, >=20 comments.
    """
    if not GH_TOKEN:
        log.warning("GH_API_TOKEN missing; skipping GitHub Discussions")
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    q = f"updated:>{since} comments:>20 sort:updated-desc"
    gql = """
    query($q: String!, $first: Int!) {
      search(query: $q, type: DISCUSSION, first: $first) {
        nodes {
          ... on Discussion {
            title
            url
            upvoteCount
            comments { totalCount }
            repository { nameWithOwner }
          }
        }
      }
    }
    """
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    try:
        r = requests.post(
            "https://api.github.com/graphql",
            json={"query": gql, "variables": {"q": q, "first": limit}},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("github fetch failed: %s", e)
        return []
    if data.get("errors"):
        log.warning("github graphql errors: %s", data["errors"])
        return []
    items = []
    for node in data.get("data", {}).get("search", {}).get("nodes", []) or []:
        if not node:
            continue
        repo = (node.get("repository") or {}).get("nameWithOwner", "")
        upv = node.get("upvoteCount", 0) or 0
        cmt = (node.get("comments") or {}).get("totalCount", 0) or 0
        items.append({
            "source": "github",
            "title": node.get("title", ""),
            "url": node.get("url", ""),
            "upvotes": upv,
            "comments": cmt,
            "views": 0,
            "meta": f"{repo} · ↑{upv} · 💬{cmt}",
        })
    return items


# ---------------- selection ----------------
def pick_top(items_by_source: dict[str, list[dict]]) -> list[dict]:
    pool: list[dict] = []
    for items in items_by_source.values():
        ranked = sorted(
            items,
            key=lambda x: unified_score(x["upvotes"], x["comments"], x.get("views")),
            reverse=True,
        )
        pool.extend(ranked[:PER_SOURCE_POOL])
    pool.sort(
        key=lambda x: unified_score(x["upvotes"], x["comments"], x.get("views")),
        reverse=True,
    )

    # Step 1: guarantee at least one per non-empty source if possible.
    selected: list[dict] = []
    seen_urls: set[str] = set()
    used_sources: set[str] = set()
    for item in pool:
        if item["source"] in used_sources or item["url"] in seen_urls:
            continue
        selected.append(item)
        seen_urls.add(item["url"])
        used_sources.add(item["source"])
        if len(used_sources) >= sum(1 for v in items_by_source.values() if v):
            break

    # Step 2: fill remaining slots by overall score.
    for item in pool:
        if len(selected) >= TOP_N:
            break
        if item["url"] in seen_urls:
            continue
        selected.append(item)
        seen_urls.add(item["url"])

    return selected[:TOP_N]


# ---------------- translation ----------------
def _is_english_dominant(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_letters = [c for c in letters if c.isascii()]
    return len(ascii_letters) / len(letters) > 0.6


def _translate_deepl(text: str) -> Optional[str]:
    if not DEEPL_KEY:
        return None
    try:
        r = requests.post(
            "https://api-free.deepl.com/v2/translate",
            data={
                "auth_key": DEEPL_KEY,
                "text": text,
                "source_lang": "EN",
                "target_lang": "KO",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["translations"][0]["text"]
    except Exception as e:
        log.warning("deepl failed (will fallback): %s", e)
        return None


def _translate_google_fallback(text: str) -> Optional[str]:
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="en", target="ko").translate(text)
    except Exception as e:
        log.warning("google fallback failed: %s", e)
        return None


def translate(text: str) -> str:
    if not text or not _is_english_dominant(text):
        return text
    return _translate_deepl(text) or _translate_google_fallback(text) or text


# ---------------- output ----------------
def build_markdown(items: list[dict]) -> str:
    date_str = TODAY_KST.strftime("%Y-%m-%d")
    lines = [
        f"# 개발자 커뮤니티 트렌드 — {date_str}",
        "",
        f"> **생성일시**: {TODAY_KST.strftime('%Y-%m-%d %H:%M KST')}",
        "> **소스**: Reddit r/programming · Stack Overflow · GitHub Discussions",
        "> **선정 기준**: 조회·추천·댓글 가중 log-scale 점수",
        "",
        "---",
        "",
    ]
    for i, it in enumerate(items, 1):
        title_ko = translate(it["title"])
        label = SOURCE_LABEL.get(it["source"], it["source"])
        lines.extend([
            f"## {i}. [{label}] {title_ko}",
            "",
            f"- **원제**: {it['title']}",
            f"- **지표**: {it['meta']}",
            f"- **링크**: {it['url']}",
            "",
        ])
    return "\n".join(lines)


def post_to_slack(items: list[dict]) -> None:
    if not SLACK_WEBHOOK:
        log.warning("SLACK_WEBHOOK_URL missing; skip slack")
        return
    date_str = TODAY_KST.strftime("%Y-%m-%d (%a)")
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📰 개발자 커뮤니티 트렌드 — {date_str}"},
        },
        {"type": "divider"},
    ]
    for i, it in enumerate(items, 1):
        title_ko = translate(it["title"])
        label = SOURCE_LABEL.get(it["source"], it["source"])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{i}. [{label}]* <{it['url']}|{title_ko}>\n  _{it['meta']}_",
            },
        })
    payload = {"text": f"개발자 커뮤니티 트렌드 — {date_str}", "blocks": blocks}
    try:
        r = requests.post(SLACK_WEBHOOK, json=payload, timeout=15)
        r.raise_for_status()
        log.info("slack post ok")
    except Exception as e:
        log.error("slack post failed: %s", e)


def write_report(md: str) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{TODAY_KST.strftime('%Y-%m-%d')}.md"
    path.write_text(md, encoding="utf-8")
    log.info("wrote %s", path)
    return path


# ---------------- main ----------------
def main() -> int:
    log.info("fetching sources...")
    sources = {
        "reddit": fetch_reddit(),
        "stackoverflow": fetch_stackoverflow(),
        "github": fetch_github_discussions(),
    }
    for k, v in sources.items():
        log.info("  %s: %d items", k, len(v))

    if sum(len(v) for v in sources.values()) == 0:
        log.error("no items from any source")
        return 1

    top = pick_top(sources)
    log.info("selected %d items", len(top))
    if not top:
        log.error("nothing selected")
        return 1

    md = build_markdown(top)
    write_report(md)
    post_to_slack(top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
