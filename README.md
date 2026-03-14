# LensMosaic

LensMosaic supports two working runtime modes from the same repository:

- `hosted_app/`: Cloud Run app for static UI, public search API, item detail API, and hosted live demo API
- `local_live/`: local ADK live API server for the blog-reader flow

## Supported Modes

### 1. Blog Reader Mode

- Open the UI from the hosted Cloud Run app
- Use the hosted public search API
- Run the live API locally from `local_live/app/main.py`

The browser uses:

- hosted origin for UI, search, and item detail requests
- local origin for live WebSocket connections

### 2. Full Hosted Demo Mode

- Open the hosted Cloud Run app directly
- Use the same origin for UI, search, item details, and live WebSockets

## What Is Working

- Similar-item search from camera frames
- Agent-triggered `find_items(...)` flows from speech and image context
- Hosted demo mode
- Hosted UI plus local live backend mode
- Desktop browser testing with the hosted UI and a local live backend

## Repository Layout

```text
lens-mosaic/
├── docs/
├── hosted_app/
│   └── app/
└── local_live/
    └── app/
```

## Start Here

- [Local reader quickstart](/Users/kaz/Documents/GitHub/lens-mosaic/docs/local-reader-quickstart.md)
- [Hosted app mobile quickstart](/Users/kaz/Documents/GitHub/lens-mosaic/docs/hosted-app-mobile-quickstart.md)
- [Cloud Run deployment](/Users/kaz/Documents/GitHub/lens-mosaic/docs/deploy-cloud-run.md)
- [Usage modes](/Users/kaz/Documents/GitHub/lens-mosaic/docs/usage-modes.md)
- [Architecture notes](/Users/kaz/Documents/GitHub/lens-mosaic/docs/architecture-plan.md)
