# LensMosaic

LensMosaic now runs as a single-origin app from the same server for:

- static UI
- search endpoints
- item detail APIs
- live WebSocket sessions

This repo also includes a smaller `blog_sample` app for tutorials and blog posts.

## Supported Mode

### Hosted App

- Open the app directly from the server you started or deployed
- Use the same origin for UI, search, item details, and live WebSockets

### Blog Sample

- Run a smaller local ADK sample with the existing hosted UI proxied from Cloud Run
- Use this when you want easier-to-read example code rather than the full app

## What Is Working

- Similar-item search from camera frames
- Agent-triggered `find_items(...)` flows from speech and image context
- Local single-server testing
- Hosted deployment

## Repository Layout

```text
lens-mosaic/
├── blog_sample/
│   └── app/
├── README.md
├── docs/
├── hosted_app/
│   ├── app/
│   ├── Dockerfile
│   ├── model_test.py
│   ├── pyproject.toml
│   └── README.md
└── ...
```

## Start Here

- [Local reader quickstart](docs/local-reader-quickstart.md)
- [Blog sample README](blog_sample/README.md)
- [Hosted app README](hosted_app/README.md)
- [Cloud Run deployment](docs/deploy-cloud-run.md)
- [Usage modes](docs/usage-modes.md)
- [Architecture notes](docs/architecture-plan.md)
