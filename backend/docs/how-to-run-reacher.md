# How to run the Reacher endpoints

The backend exposes two FastAPI routes that hit the [Reacher Social
Intelligence API](https://api.reacherapp.com):

| Route | What it returns |
|---|---|
| `GET /api/trending-videos` | Top-performing TikTok Shop videos matching an interest. |
| `GET /api/competitors` | Catalog matches for a free-text product description, plus the top creators and videos per match. |

Implementation lives in [`backend/reacher.py`](../reacher.py); routes are
wired in [`backend/main.py`](../main.py).

---

## 1. Set up credentials

Both endpoints read two environment variables. The simplest way is a
local `.env` file.

```bash
cp backend/.env.example backend/.env
```

Then edit `backend/.env`:

```ini
REACHER_API_KEY=your_reacher_api_key
REACHER_SHOP_ID=1079
```

`backend/.env` is gitignored. `python-dotenv` loads it automatically when
the FastAPI app starts (see `load_dotenv()` at the top of
`backend/main.py`), so you do **not** need to `source` anything.

### Picking a `REACHER_SHOP_ID`

Reacher API keys can have access to multiple shops. Run the
`list_shops` MCP tool (or `GET https://api.reacherapp.com/public/v1/shops`
with your API key) to see yours. Examples on this account:

| shop_id | name | region | status |
|---|---|---|---|
| 1183 | Grocery Stars | US | pro_new |
| 1079 | Reacher Fincorp USA | US | pro |
| 1080 | Reacher Pharma UK | UK | pro |

Rules of thumb:

- `/api/competitors` (and the per-product creators/videos endpoints it
  calls) **require a single shop_id** — `all` and comma-separated lists
  are rejected.
- `/api/trending-videos` accepts a single id **or** the literal `all` to
  aggregate across every shop the key can see.

If you want to query a different shop without restarting, set
`REACHER_SHOP_ID` to your most-common shop and ask for a per-request
`?shop=` override to be added.

---

## 2. Start the server

```bash
cd backend
uv sync           # first time only
uv run uvicorn main:app --reload --port 8000
```

Sanity check:

```bash
curl http://localhost:8000/api/hello
# -> {"message":"f from FastAPI"}
```

Interactive Swagger UI is available at
<http://localhost:8000/docs> — you can fire both Reacher endpoints from
there without writing any curl.

---

## 3. `GET /api/trending-videos`

Returns the trending TikTok Shop videos that match `interest`.

### Query parameters

| Param | Default | Notes |
|---|---|---|
| `interest` | _required_ | Free text, max 200 chars. Mapped to Reacher's `search`. |
| `page` | `1` | |
| `page_size` | `20` | 1–100 |
| `category` | _none_ | Max 100 chars, optional refinement. |
| `sort_by` | `views` | One of `gmv`, `views`, `likes`, `engagement`, `date`. |
| `sort_order` | `desc` | `asc` or `desc`. |
| `time_range` | `7 days` | One of `1 day`, `7 days`, `8 days`, `14 days`, `28 days`, `30 days`, `90 days`, `180 days`, `365 days`, `all`. |

### Example

```bash
curl "http://localhost:8000/api/trending-videos?interest=skincare&page_size=10&time_range=30%20days"
```

The response is the raw Reacher payload — typically `{ "data": [ ...videos ] }` plus pagination metadata.

---

## 4. `GET /api/competitors`

Given a free-text product description, this:

1. Searches the TikTok Shop catalog for matching products (the
   "competitor" set).
2. For each match, concurrently fetches the top creators promoting it
   and the top videos featuring it.

The semantic match is delegated to Reacher's product `search` (fuzzy /
keyword based — not embedding semantics). For most hackathon-style
queries like "glossy pink lip oil" or "matcha protein powder" this
returns sensible results.

### Query parameters

| Param | Default | Notes |
|---|---|---|
| `query` | _required_ | Free text, max 200 chars. |
| `top_products` | `5` | 1–20 competitor products to return. |
| `creators_per_product` | `10` | 1–50 |
| `videos_per_product` | `10` | 1–50 |
| `time_range` | `30 days` | Window applied to each product's videos. Same allowed values as `/api/trending-videos`. |

### Example

```bash
curl "http://localhost:8000/api/competitors?query=glossy+pink+lip+oil&top_products=3&time_range=30%20days"
```

### Response shape

```jsonc
{
  "query": "glossy pink lip oil",
  "competitor_count": 3,
  "competitors": [
    {
      "product": { /* full Reacher product object */ },
      "creators": [ /* top creators driving sales for this product */ ],
      "videos":   [ /* top videos featuring this product */ ]
    },
    // ...
  ]
}
```

If a single product's creators or videos call fails, that slot is
returned as `{"error": "..."}` instead of taking down the whole
response (the orchestrator uses `asyncio.gather(..., return_exceptions=True)`).

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `500: REACHER_API_KEY and REACHER_SHOP_ID must be set` | `.env` missing or not loaded | Confirm `backend/.env` exists; restart `uvicorn` (it only loads on startup). |
| `4xx` from `/api/competitors` mentioning "shop" | `REACHER_SHOP_ID=all` was passed | Per-product endpoints need a single shop_id. Set it to one of the active ids returned by `list_shops`. |
| Empty `competitors` array | The product `search` found no matches | Loosen the query — generic category words ("lip gloss") work better than full marketing taglines. |
| `pattern` validation error on `time_range` | Used a value not in the allow-list | Allowed values are listed in §3 / §4. Note the literal space, e.g. `7 days` not `7d`. |

---

## 6. Files involved

- `backend/main.py` — FastAPI routes, env loading, request validation.
- `backend/reacher.py` — async HTTP client, error types, orchestrator.
- `backend/.env.example` — template for credentials.
- `backend/pyproject.toml` — declares `httpx` and `python-dotenv` (loaded via `uv sync`).
