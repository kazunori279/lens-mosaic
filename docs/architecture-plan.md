# Architecture Notes

## Goals

LensMosaic needs to support two modes from the same codebase:

1. Blog reader mode
   - Hosted UI
   - Hosted public search API
   - Local live API server run by the reader
2. Public demo mode
   - Hosted UI
   - Hosted public search API
   - Hosted live API server

## Current Repo Layout

```text
lens-mosaic/
├── hosted_app/
│   └── app/
│       ├── main.py
│       └── static/
├── local_live/
│   └── app/
│       └── main.py
├── docs/
└── README.md
```

## Responsibilities

### `hosted_app`

- Serves static UI assets
- Exposes public search endpoints
- Can also expose hosted live WebSocket endpoints for quick demos

### `local_live`

- Stays focused on the live ADK server used in the blog post
- Uses the hosted search service instead of local Vector Search setup
- Targets desktop-browser testing with the hosted UI

## Refactor Steps

1. Add frontend runtime config for `searchOrigin` and `liveOrigin`
2. Make the hosted UI default to same-origin hosted demo mode
3. Add a query-param or bootstrap config for local-live mode
4. Consolidate or copy shared static assets intentionally
5. Move search-service settings to environment variables
6. Add deployment docs for Cloud Run

## Current Implementation Notes

- `hosted_app` serves static files, search endpoints, item detail endpoints, and hosted live WebSocket endpoints.
- `local_live` serves the live WebSocket endpoints and delegates search to the hosted service through `SEARCH_SERVICE_URL`.
- The frontend switches live origin with the `backend=` query parameter.
- The `find_items(...)` tool path now performs its search work directly instead of round-tripping through the main event loop, which avoids stalled live turns after image-based requests.
