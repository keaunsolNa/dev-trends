"""
Daily Dev Community Trends fetcher (extractive summary, no LLM cost).

Sources:
  - Hacker News top stories (Firebase API)
  - Stack Overflow (Stack Exchange API, hot)
  - GitHub Discussions (GraphQL search)

Summarization: extract first N meaningful sentences -> translate via DeepL.
To upgrade to LLM-based summary later, replace _build_summary() body
with a call to an LLM API (see commented _summarize_with_llm template).
"""
from __future__ import annotations

import concurrent.futures
import html as html_mod
import logging
import math
import os
import re
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
PER_SOURCE_POOL = 3

DEEPL_KEY = os.getenv("DEEPL_API_KEY")
GH_TOKEN = os.getenv("GH_API_TOKEN")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")
SO_KEY = os.getenv("STACK_EXCHANGE_KEY")

USER_AGENT = "dev-trends-bot/1.0 (by GitHub Actions)"
HEADERS_COMMON = {"User-Agent": USER_AGENT}

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
SUMMARY_MAX_CHARS = 300  # final Korean output cap
EXTRACT_MAX_CHARS = 500  # cap for DeepL input (saves quota)
EXTRACT_MAX_SENTENCES = 3

SOURCE_LABEL = {
    "hackernews": "Hacker News",
    "stackoverflow": "Stack Overflow",
    "github": "GitHub Discussions",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trends")


# ---------------- utilities ----------------
def unified_score(upvotes: int, comments: int, views: Optional[int] = None) -> float:
    u = max(upvotes or 0, 0)
    c = max(comments or 0, 0)
    v = max(views or 0, 0)
    s = math.log10(u + 1) * 1.0 + math.log10(c + 1) * 1.5
    if v > 0:
        s += math.log10(v + 1) * 0.3
    return s


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<pre>.*?</pre>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<code>.*?</code>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------- sources ----------------
def _fetch_hn_item(item_id: int) -> Optional[dict]:
    try:
        r = requests.get(f"{HN_API_BASE}/item/{item_id}.json", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("hn item %s fetch failed: %s", item_id, e)
        return None


def _hn_body(item: dict) -> str:
    """Self-post text, or first top-level comment, or ''."""
    text = strip_html(item.get("text") or "")
    if text:
        return text
    kids = item.get("kids") or []
    if not kids:
        return ""
    top = _fetch_hn_item(kids[0])
    if top and top.get("text"):
        return strip_html(top["text"])
    return ""


def fetch_hackernews(limit: int = 25, pool: int = 50) -> list[dict]:
    try:
        r = requests.get(f"{HN_API_BASE}/topstories.json", timeout=15)
        r.raise_for_status()
        ids = r.json()[:pool]
    except Exception as e:
        log.warning("hn topstories fetch failed: %s", e)
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(_fetch_hn_item, ids))

    items: list[dict] = []
    for result in results:
        if not result:
            continue
        if result.get("type") != "story":
            continue
        if result.get("dead") or result.get("deleted"):
            continue
        hn_id = result.get("id")
        title = result.get("title", "")
        score = result.get("score", 0) or 0
        comments = result.get("descendants", 0) or 0
        hn_url = f"https://news.ycombinator.com/item?id={hn_id}"
        items.append({
            "source": "hackernews",
            "title": title,
            "url": hn_url,
            "upvotes": score,
            "comments": comments,
            "views": 0,
            "meta": f"HN · ↑{score} · 💬{comments}",
            "_raw": result,  # defer body extraction until selected
        })
        if len(items) >= limit:
            break
    return items


def fetch_stackoverflow(limit: int = 25) -> list[dict]:
    url = "https://api.stackexchange.com/2.3/questions"
    params = {
        "order": "desc",
        "sort": "hot",
        "site": "stackoverflow",
        "pagesize": limit,
        "filter": "withbody",
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
        body = strip_html(q.get("body", ""))
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
            "body": body,
        })
    return items


def fetch_github_discussions(limit: int = 25) -> list[dict]:
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
            bodyText
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
        body = (node.get("bodyText") or "").strip()
        items.append({
            "source": "github",
            "title": node.get("title", ""),
            "url": node.get("url", ""),
            "upvotes": upv,
            "comments": cmt,
            "views": 0,
            "meta": f"{repo} · ↑{upv} · 💬{cmt}",
            "body": body,
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

    selected: list[dict] = []
    seen_urls: set[str] = set()
    used_sources: set[str] = set()
    non_empty_sources = sum(1 for v in items_by_source.values() if v)
    for item in pool:
        if item["source"] in used_sources or item["url"] in seen_urls:
            continue
        selected.append(item)
        seen_urls.add(item["url"])
        used_sources.add(item["source"])
        if len(used_sources) >= non_empty_sources:
            break
    for item in pool:
        if len(selected) >= TOP_N:
            break
        if item["url"] in seen_urls:
            continue
        selected.append(item)
        seen_urls.add(item["url"])
    return selected[:TOP_N]


# ---------------- summarization (extractive, no LLM) ----------------
def _resolve_body(item: dict) -> str:
    """Lazily resolve body. For HN, may fetch a comment."""
    if "body" in item and item["body"] is not None:
        return item["body"]
    if item["source"] == "hackernews" and "_raw" in item:
        body = _hn_body(item["_raw"])
        item["body"] = body
        return body
    return ""


def _extract_first_sentences(body: str) -> str:
    """Take first N meaningful sentences, drop code-ish/URL-only lines."""
    if not body:
        return ""
    text = re.sub(r"\s+", " ", body).strip()
    # split on sentence terminators + whitespace
    raw = re.split(r"(?<=[.!?])\s+", text)
    picked: list[str] = []
    for s in raw:
        s = s.strip()
        if len(s) < 20:
            continue
        # drop pure URL/code-like fragments
        if s.startswith(("http://", "https://", "$ ", "> ", "```")):
            continue
        # drop lines that are mostly non-alpha (e.g., stack traces, tables)
        alpha = sum(1 for c in s if c.isalpha())
        if alpha / max(len(s), 1) < 0.5:
            continue
        picked.append(s)
        if len(picked) >= EXTRACT_MAX_SENTENCES:
            break
    out = " ".join(picked)
    if len(out) > EXTRACT_MAX_CHARS:
        out = out[: EXTRACT_MAX_CHARS - 3].rsplit(" ", 1)[0] + "..."
    return out


def _build_summary(item: dict) -> Optional[str]:
    """Extract + translate. Returns None if body is empty."""
    body = _resolve_body(item).strip()
    if not body:
        return None
    extract = _extract_first_sentences(body)
    if not extract:
        return None
    translated = translate(extract)
    if len(translated) > SUMMARY_MAX_CHARS:
        translated = translated[: SUMMARY_MAX_CHARS - 3].rsplit(" ", 1)[0] + "..."
    return translated


def summarize_items(items: list[dict]) -> None:
    for it in items:
        summary = _build_summary(it)
        it["summary"] = summary or "(본문 없음 — 원문 링크 참조)"


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
        "> **소스**: Hacker News · Stack Overflow · GitHub Discussions",
        "> **선정 기준**: 조회·추천·댓글 가중 log-scale 점수",
        "> **요약 방식**: 본문 첫 문장 추출 + DeepL 번역 (LLM 미사용)",
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
            f"- **요약**: {it.get('summary', '')}",
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
        summary = it.get("summary", "")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{i}. [{label}]* <{it['url']}|{title_ko}>\n"
                    f"  _{it['meta']}_\n"
                    f"  📝 {summary}"
                ),
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


# ---------------- upgrade path ----------------
# To switch to LLM-based abstractive summary later:
#   1) Add ANTHROPIC_API_KEY (or OPENAI_API_KEY) to secrets & workflow env
#   2) Replace _build_summary() with a call to the LLM
#   3) Example stub below.
#
# def _summarize_with_llm(title, body, label) -> Optional[str]:
#     # POST to https://api.anthropic.com/v1/messages with model claude-haiku-4-5
#     # and a prompt requesting a ~300-char Korean summary.
#     ...


# ---------------- main ----------------
def main() -> int:
    log.info("fetching sources...")
    sources = {
        "hackernews": fetch_hackernews(),
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

    log.info("summarizing top %d (extractive)...", len(top))
    summarize_items(top)

    md = build_markdown(top)
    write_report(md)
    post_to_slack(top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
