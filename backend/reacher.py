"""Reacher Social Intelligence API client.

Wraps the trending videos endpoint so the FastAPI app can fetch the
top-performing TikTok Shop videos that match a user-supplied interest.

Required env vars:
    REACHER_API_KEY  - x-api-key header
    REACHER_SHOP_ID  - x-shop-id header (use 'all' to aggregate every shop
                       this key has access to)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

REACHER_BASE_URL = "https://api.reacherapp.com/public/v1"
TRENDING_PATH = "/social-intelligence/videos/trending"
PRODUCTS_PATH = "/social-intelligence/products"

ALLOWED_TIME_RANGES = {
    "1 day", "7 days", "8 days", "14 days", "28 days",
    "30 days", "90 days", "180 days", "365 days", "all",
}
ALLOWED_SORT_BY = {"gmv", "views", "likes", "engagement", "date"}


class ReacherConfigError(RuntimeError):
    pass


class ReacherAPIError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"Reacher API returned {status}: {body}")
        self.status = status
        self.body = body


def _headers() -> dict[str, str]:
    api_key = os.environ.get("REACHER_API_KEY")
    shop_id = os.environ.get("REACHER_SHOP_ID")
    if not api_key or not shop_id:
        raise ReacherConfigError(
            "REACHER_API_KEY and REACHER_SHOP_ID must be set in the environment."
        )
    return {"x-api-key": api_key, "x-shop-id": shop_id}


async def get_trending_videos(
    interest: str,
    *,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    sort_by: str = "views",
    sort_order: str = "desc",
    time_range: str = "7 days",
) -> dict[str, Any]:
    """Fetch trending TikTok Shop videos matching `interest`."""

    if sort_by not in ALLOWED_SORT_BY:
        raise ValueError(f"sort_by must be one of {sorted(ALLOWED_SORT_BY)}")
    if sort_order not in {"asc", "desc"}:
        raise ValueError("sort_order must be 'asc' or 'desc'")
    if time_range not in ALLOWED_TIME_RANGES:
        raise ValueError(f"time_range must be one of {sorted(ALLOWED_TIME_RANGES)}")

    params: dict[str, Any] = {
        "page": page,
        "page_size": max(1, min(page_size, 100)),
        "sort_by": sort_by,
        "sort_order": sort_order,
        "time_range": time_range,
    }
    if interest:
        params["search"] = interest[:200]
    if category:
        params["category"] = category[:100]

    async with httpx.AsyncClient(base_url=REACHER_BASE_URL, timeout=30.0) as client:
        resp = await client.get(TRENDING_PATH, params=params, headers=_headers())

    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise ReacherAPIError(resp.status_code, body)

    return resp.json()


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    clean = {k: v for k, v in params.items() if v is not None}
    resp = await client.get(path, params=clean, headers=_headers())
    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise ReacherAPIError(resp.status_code, body)
    return resp.json()


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Reacher list endpoints have varied envelopes — pull the array out."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "products", "creators", "videos"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _product_id(product: dict[str, Any]) -> str | None:
    for key in ("product_id", "productId", "id"):
        v = product.get(key)
        if v:
            return str(v)
    return None


async def search_products(
    query: str,
    *,
    page: int = 1,
    page_size: int = 10,
    category: str | None = None,
    subcategory: str | None = None,
    sort_by: str = "gmv28d",
    sort_order: str = "desc",
) -> dict[str, Any]:
    """Search the TikTok Shop catalog. `query` maps to Reacher's `search` param."""
    params = {
        "page": page,
        "page_size": max(1, min(page_size, 100)),
        "search": (query or "")[:200] or None,
        "category": category[:100] if category else None,
        "subcategory": subcategory[:100] if subcategory else None,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    async with httpx.AsyncClient(base_url=REACHER_BASE_URL, timeout=30.0) as client:
        return await _get(client, PRODUCTS_PATH, params)


async def get_product_creators(
    product_id: str,
    *,
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "gmv28d",
    sort_order: str = "desc",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    params = {
        "page": page,
        "page_size": max(1, min(page_size, 100)),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    path = f"{PRODUCTS_PATH}/{product_id}/creators"
    if client is not None:
        return await _get(client, path, params)
    async with httpx.AsyncClient(base_url=REACHER_BASE_URL, timeout=30.0) as c:
        return await _get(c, path, params)


async def get_product_videos(
    product_id: str,
    *,
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "views",
    sort_order: str = "desc",
    time_range: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if time_range and time_range not in ALLOWED_TIME_RANGES:
        raise ValueError(f"time_range must be one of {sorted(ALLOWED_TIME_RANGES)}")
    params = {
        "page": page,
        "page_size": max(1, min(page_size, 100)),
        "sort_by": sort_by,
        "sort_order": sort_order,
        "time_range": time_range,
    }
    path = f"{PRODUCTS_PATH}/{product_id}/videos"
    if client is not None:
        return await _get(client, path, params)
    async with httpx.AsyncClient(base_url=REACHER_BASE_URL, timeout=30.0) as c:
        return await _get(c, path, params)


async def get_competitor_landscape(
    query: str,
    *,
    top_products: int = 5,
    creators_per_product: int = 10,
    videos_per_product: int = 10,
    time_range: str | None = "30 days",
) -> dict[str, Any]:
    """Semantic-ish "what are my competitors doing" view.

    1. Search the catalog for `query` (e.g. a lip-gloss product description).
    2. For each top match, concurrently fetch the top creators promoting it
       and the top videos featuring it.
    """
    headers = _headers()  # raises early if env missing
    async with httpx.AsyncClient(
        base_url=REACHER_BASE_URL, timeout=30.0, headers=headers,
    ) as client:
        product_payload = await _get(
            client,
            PRODUCTS_PATH,
            {
                "page": 1,
                "page_size": max(1, min(top_products, 100)),
                "search": (query or "")[:200] or None,
                "sort_by": "gmv28d",
                "sort_order": "desc",
            },
        )
        products = _extract_items(product_payload)[:top_products]

        async def fetch_for(product: dict[str, Any]) -> dict[str, Any]:
            pid = _product_id(product)
            if not pid:
                return {"product": product, "error": "product has no id field"}
            creators_task = get_product_creators(
                pid, page_size=creators_per_product, client=client,
            )
            videos_task = get_product_videos(
                pid,
                page_size=videos_per_product,
                time_range=time_range,
                client=client,
            )
            results = await asyncio.gather(
                creators_task, videos_task, return_exceptions=True,
            )
            creators_res, videos_res = results
            return {
                "product": product,
                "creators": (
                    {"error": str(creators_res)}
                    if isinstance(creators_res, Exception)
                    else _extract_items(creators_res)
                ),
                "videos": (
                    {"error": str(videos_res)}
                    if isinstance(videos_res, Exception)
                    else _extract_items(videos_res)
                ),
            }

        competitors = await asyncio.gather(*(fetch_for(p) for p in products))

    return {"query": query, "competitor_count": len(competitors), "competitors": competitors}
