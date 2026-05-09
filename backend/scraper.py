import asyncio
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
import httpx
from pydantic import BaseModel
from browser_use import Agent, Browser
from browser_use.llm.models import ChatOpenAI

load_dotenv(Path(__file__).parent / ".env")


class Mention(BaseModel):
    platform: Literal["x", "linkedin", "reddit"]
    post_id: str
    post_url: str
    author_handle: str
    author_display_name: str
    post_text: str
    subreddit: str | None = None
    post_type: Literal["post", "comment"] | None = None
    posted_at: datetime | None = None
    likes: int | None = None
    reposts: int | None = None
    comments: int | None = None
    matched_terms: list[str] = []


class _MentionList(BaseModel):
    mentions: list[Mention]


# ── shared helpers ────────────────────────────────────────────────────────────

def _matched_terms(text: str, brand_terms: list[str]) -> list[str]:
    return [t for t in brand_terms if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)]


def _filter(
    mentions: list[Mention],
    brand_terms: list[str],
    lookback_minutes: int,
    seen_ids: set[str],
) -> list[Mention]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    out: list[Mention] = []
    for m in mentions:
        if m.post_id in seen_ids:
            continue
        if m.posted_at is not None:
            ts = m.posted_at if m.posted_at.tzinfo else m.posted_at.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        matched = _matched_terms(m.post_text, brand_terms)
        if not matched:
            continue
        m.matched_terms = matched
        out.append(m)
    return out


# ── browser-based scrapers ────────────────────────────────────────────────────

_AUTH_DIR = Path(__file__).parent / "auth"


async def _browser_scrape(
    platform: Literal["x", "linkedin"],
    url: str,
    brand_terms: list[str],
    lookback_minutes: int,
    seen_ids: set[str],
) -> list[Mention]:
    prompt = (
        f"Navigate to {url}. "
        f"Extract all visible posts mentioning any of: {brand_terms}. "
        "For each post collect: post_id (from the post URL), post_url, author_handle, "
        "author_display_name, post_text, posted_at (ISO 8601), likes, reposts, comments. "
        "Do NOT log in, click into individual posts, or engage with any content. "
        "If you encounter an auth wall, CAPTCHA, or any blocker, return an empty mentions list."
    )
    state_file = _AUTH_DIR / f"{platform}_state.json"
    try:
        llm = ChatOpenAI(model="gpt-4o")
        browser = Browser(
            storage_state=str(state_file) if state_file.exists() else None,
            user_data_dir=None,
        )
        agent = Agent(
            task=prompt,
            llm=llm,
            browser=browser,
            output_model_schema=_MentionList,
        )
        result = await asyncio.wait_for(agent.run(), timeout=180)
        raw: _MentionList | None = (
            result.final_result() if hasattr(result, "final_result") else result
        )
        if not isinstance(raw, _MentionList):
            print(f"[scrape_{platform}] parse failure: unexpected result type {type(raw)}")
            return []
        for m in raw.mentions:
            m.platform = platform
        return _filter(raw.mentions, brand_terms, lookback_minutes, seen_ids)
    except asyncio.TimeoutError:
        print(f"[scrape_{platform}] timeout after 90s")
        return []
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("captcha", "auth", "login", "sign in")):
            print(f"[scrape_{platform}] auth/captcha wall: {exc}")
        else:
            print(f"[scrape_{platform}] error: {exc}")
        return []


async def scrape_x(
    brand_terms: list[str], lookback_minutes: int, seen_ids: set[str]
) -> list[Mention]:
    q = urllib.parse.quote_plus(" OR ".join(brand_terms))
    return await _browser_scrape(
        "x", f"https://x.com/search?q={q}&f=live", brand_terms, lookback_minutes, seen_ids
    )


async def scrape_linkedin(
    brand_terms: list[str], lookback_minutes: int, seen_ids: set[str]
) -> list[Mention]:
    q = urllib.parse.quote_plus(" ".join(brand_terms))
    url = f"https://www.linkedin.com/search/results/content/?keywords={q}&sortBy=date_posted"
    return await _browser_scrape("linkedin", url, brand_terms, lookback_minutes, seen_ids)


# ── Reddit scraper ────────────────────────────────────────────────────────────

_REDDIT_SEM = asyncio.Semaphore(1)
_REDDIT_UA = "brand-monitor/0.1 by /u/placeholder"


async def _reddit_fetch(
    client: httpx.AsyncClient, term: str, search_type: str | None
) -> list[dict]:
    async with _REDDIT_SEM:
        params: dict[str, str | int] = {"q": term, "sort": "new", "t": "day", "limit": 100}
        if search_type:
            params["type"] = search_type
        try:
            r = await client.get("https://www.reddit.com/search.json", params=params)
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
        except Exception:
            children = []
        await asyncio.sleep(1)  # rate-limit: holds semaphore during sleep
    return children


async def scrape_reddit(
    brand_terms: list[str], lookback_minutes: int, seen_ids: set[str]
) -> list[Mention]:
    headers = {"User-Agent": _REDDIT_UA}
    mentions: list[Mention] = []
    deduped: set[str] = set()

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for term in brand_terms:
            for is_comment in (False, True):
                children = await _reddit_fetch(client, term, "comment" if is_comment else None)
                for child in children:
                    d = child.get("data", {})
                    post_id: str = d.get("name", "")
                    if not post_id or post_id in deduped:
                        continue
                    deduped.add(post_id)

                    permalink = d.get("permalink", "")
                    post_url = f"https://www.reddit.com{permalink}"

                    if is_comment:
                        post_text = d.get("body", "")
                        post_type: Literal["post", "comment"] = "comment"
                    else:
                        title = d.get("title", "")
                        selftext = d.get("selftext", "")
                        post_text = f"{title}\n\n{selftext}".rstrip()
                        post_type = "post"

                    created_utc = d.get("created_utc")
                    posted_at = (
                        datetime.fromtimestamp(created_utc, tz=timezone.utc)
                        if created_utc
                        else None
                    )

                    mentions.append(
                        Mention(
                            platform="reddit",
                            post_id=post_id,
                            post_url=post_url,
                            author_handle=d.get("author", ""),
                            author_display_name=d.get("author", ""),
                            post_text=post_text,
                            subreddit=d.get("subreddit"),
                            post_type=post_type,
                            posted_at=posted_at,
                            likes=d.get("score"),
                            reposts=None,
                            comments=d.get("num_comments"),
                            matched_terms=[],
                        )
                    )

    return _filter(mentions, brand_terms, lookback_minutes, seen_ids)
