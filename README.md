# LensMosaic

LensMosaic now lives in a dedicated repository with two app entrypoints:

- `hosted_app/`: the Cloud Run app that serves the UI, public search API, and hosted live demo API
- `local_live/`: the local live API server used in the blog post flow

## Supported Modes

### 1. Blog reader mode

- Open the UI from the hosted Cloud Run app
- Use the hosted public search API
- Run the live API locally from `local_live/app/main.py`

In this mode, the browser uses:

- hosted origin for search and item detail requests
- local origin for live WebSocket connections

### 2. Full hosted demo mode

- Open the UI from the hosted Cloud Run app
- Use the hosted public search API
- Use the hosted live API from the same Cloud Run service

In this mode, everything is same-origin.

## Repository Layout

```text
lens-mosaic/
├── docs/
├── hosted_app/
│   └── app/
└── local_live/
    └── app/
```

## Current Status

The repo has been migrated from the earlier prototype directories and the hosted app now contains:

- static UI serving
- public search endpoints
- hosted live WebSocket endpoints

The frontend has also been updated so it can choose separate `searchOrigin` and `liveOrigin` at runtime.

See `docs/architecture-plan.md` for the target architecture and `docs/usage-modes.md` for the runtime model.
