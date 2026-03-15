# LensMosaic

LensMosaic now runs as a single-origin app from the same server for:

- static UI
- search endpoints
- item detail APIs
- live WebSocket sessions

## Supported Mode

### Hosted App

- Open the app directly from the server you started or deployed
- Use the same origin for UI, search, item details, and live WebSockets

## What Is Working

- Similar-item search from camera frames
- Agent-triggered `find_items(...)` flows from speech and image context
- Local single-server testing
- Hosted deployment

## Repository Layout

```text
lens-mosaic/
├── docs/
└── hosted_app/
    └── app/
```

## Start Here

- [Local reader quickstart](/Users/kaz/Documents/GitHub/lens-mosaic/docs/local-reader-quickstart.md)
- [Hosted app README](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/README.md)
- [Cloud Run deployment](/Users/kaz/Documents/GitHub/lens-mosaic/docs/deploy-cloud-run.md)
- [Usage modes](/Users/kaz/Documents/GitHub/lens-mosaic/docs/usage-modes.md)
- [Architecture notes](/Users/kaz/Documents/GitHub/lens-mosaic/docs/architecture-plan.md)
