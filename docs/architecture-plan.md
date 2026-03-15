# Architecture Notes

## Goals

LensMosaic now targets a single-origin architecture from the same codebase:

1. Local development mode
   - Local hosted UI
   - Local search API
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
- The live agent uses a single FastAPI process with WebSocket endpoints for:
  - live conversation at `/ws/{user_id}/{session_id}`
  - tile updates at `/ws_image_tile/{user_id}`
- Search and conversation are now intentionally split:
  - text and recommendation searches still run synchronously in-process
  - camera-driven similar-item search runs on a dedicated background worker pool
  - the main asyncio loop stays focused on live audio/text traffic and WebSocket fan-out
- Camera frames are handled as two independent features per session:
  - `similar_search_enabled`: whether frames should drive the similar-item search worker
  - `agent_vision_enabled`: whether frames should be forwarded into the live Gemini session
- The local UI exposes those two controls behind the header status button:
  - `Similar search`
  - `Agent vision`
  - This lets us test `search only`, `vision only`, `both`, or `neither` without code changes.
- Similar-item search worker design:
  - one process-wide queue and a configurable worker pool
  - one latest-image slot per user session
  - no attempt to process every frame in order
  - if a new frame arrives while a search is running, the worker requeues that session and searches the latest frame next
  - completed search results are posted back to the main loop for WebSocket broadcast to the tile UI
- The `find_items(...)` tool path performs its search work directly instead of round-tripping through the main event loop, which avoids stalled live turns after text-initiated item requests.

## Session Model

- `UserSession` holds:
  - the latest camera frame
  - current similar-item results
  - current recommended-item results
  - connected tile WebSocket clients
  - camera-mode flags for search and vision
  - worker-thread coordination state for latest-frame-only image search
- Live conversation state is managed separately through ADK session storage and the live request queue.

## Current Tradeoffs

- Similar-item search remains in the same server process, but no longer blocks the main event loop.
- Agent vision is optional because continuous image forwarding can noticeably increase live conversation latency.
- The current worker model prefers freshness over completeness:
  - latest frame wins
  - intermediate frames may be skipped
  - this is desirable for camera-driven search UX

## Near-Term Direction

1. Keep `similar_search_enabled` and `agent_vision_enabled` as separate controls and concepts
2. Prefer agent vision off by default unless the user explicitly needs the live model to inspect the camera feed
3. Keep similar-item search off the event loop
4. If more throughput is needed later, move image search to a separate process or service without changing the browser contract
