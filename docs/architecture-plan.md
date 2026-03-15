# Architecture Notes

## Goals

LensMosaic now targets a single-origin architecture from the same codebase:

1. Local development mode
   - Local hosted UI
   - Local search API
   - Local live API server
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
- Exposes search endpoints
- Exposes item detail APIs
- Exposes live WebSocket endpoints

## Current Direction

1. Keep the browser on a single origin for UI, search, item details, and live sockets
2. Use `hosted_app` as the local and deployed server entrypoint
3. Keep deployment docs focused on one-server startup

## Current Implementation Notes

- `hosted_app` serves static files, search endpoints, item detail endpoints, and live WebSocket endpoints.
- The browser now talks to the same origin for every app capability.
- The `find_items(...)` tool path performs its search work directly instead of round-tripping through the main event loop, which avoids stalled live turns after image-based requests.
