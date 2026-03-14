# Usage Modes

## Full Hosted Demo Mode

Open the hosted app with no special query parameters.

Example:

```text
https://lens-mosaic-demo-xxxxx.run.app/
```

The UI will use the same origin for:

- static assets
- search endpoints
- item detail lookups
- live WebSocket endpoints

## Blog Reader Mode

Open the hosted app and point its live backend at a local server with the `backend` query parameter.

Examples:

```text
https://lens-mosaic-demo-xxxxx.run.app/?backend=http://127.0.0.1:8000
https://lens-mosaic-demo-xxxxx.run.app/?backend=https://192.168.1.10:8000
```

Optional `search` override:

```text
https://lens-mosaic-demo-xxxxx.run.app/?backend=http://127.0.0.1:8000&search=https://lens-mosaic-demo-xxxxx.run.app
```

The UI will use:

- `backend` for `/ws/...` and `/ws_image_tile/...`
- the hosted origin for `/api/item/...`

## Notes

- Desktop localhost flows are the easiest for blog readers.
- Phone and tablet readers need the local live server reachable on a LAN URL if they are not using the same device that runs the backend.
- The local live server should be configured to call the hosted search API through `SEARCH_SERVICE_URL`.
- If the local live server is used from an iPad or phone, prefer `https://YOUR_LAN_IP:8000` rather than plain HTTP.
- The current implementation has been smoke-tested for similar-item search and agent `find_items(...)` responses in both modes.
