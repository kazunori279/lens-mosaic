# Architecture Plan

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

## Proposed Repo Layout

```text
lens-mosaic/
├── hosted_app/
│   └── app/
│       ├── main.py
│       └── static/
├── local_live/
│   └── app/
│       ├── main.py
│       └── certs/
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
- Keeps local HTTPS support for camera and microphone testing

## Refactor Steps

1. Add frontend runtime config for `searchOrigin` and `liveOrigin`
2. Make the hosted UI default to same-origin hosted demo mode
3. Add a query-param or bootstrap config for local-live mode
4. Consolidate or copy shared static assets intentionally
5. Move search-service settings to environment variables
6. Add deployment docs for Cloud Run
