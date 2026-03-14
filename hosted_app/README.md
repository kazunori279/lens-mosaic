# Hosted App

`hosted_app` is the Cloud Run service for LensMosaic.

It currently serves:

- static UI assets
- public search endpoints
- item detail endpoints for the UI
- hosted live WebSocket endpoints for the quick demo

This app is intended to support both:

1. same-origin hosted demo mode
2. hosted UI plus local live backend mode

For local testing on a phone or tablet, run `hosted_app` over HTTPS with your own
locally generated certificate files. See
[docs/hosted-app-mobile-quickstart.md](/Users/kaz/Documents/GitHub/lens-mosaic/docs/hosted-app-mobile-quickstart.md).
