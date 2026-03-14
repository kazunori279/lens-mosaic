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

## Local Start

Configure the app first:

```bash
cd hosted_app/app
cp .env.example .env
```

Then start it locally:

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app --host 127.0.0.1 --port 8081
```

## Local Test

### Hosted-only mode

Open:

```text
http://127.0.0.1:8081/
```

Quick checks:

- `http://127.0.0.1:8081/health`
- `http://127.0.0.1:8081/`

### Hosted UI plus local_live mode

Start `local_live` with the hosted app as its search backend:

```bash
cd local_live/app
SEARCH_SERVICE_URL=http://127.0.0.1:8081 \
  uv run --project .. uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8081/?backend=http://127.0.0.1:8000
```

Quick checks:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/`
- `curl -X POST http://127.0.0.1:8081/search -H 'content-type: application/json' -d '{"text":"speaker"}'`

Expected behavior:

- the hosted app keeps serving the UI and search endpoints
- `local_live` handles `/ws/...` and `/ws_image_tile/...`
- item details still resolve through the hosted app

For local testing on a phone or tablet, run `hosted_app` over HTTPS with your own
locally generated certificate files. See
[docs/hosted-app-mobile-quickstart.md](/Users/kaz/Documents/GitHub/lens-mosaic/docs/hosted-app-mobile-quickstart.md).

For phone or tablet testing, make sure the HTTPS server is started with `--host 0.0.0.0`
so the app is reachable from other devices on your LAN.

After the HTTPS server is running, you can generate a QR code for the phone URL:

```bash
python3 - <<'PY'
from urllib.parse import quote
from urllib.request import urlopen
from pathlib import Path

url = "https://YOUR_LAN_IP:8081/"
qr_url = f"http://api.qrserver.com/v1/create-qr-code/?size=480x480&data={quote(url, safe='')}"
out = Path("/tmp/lens-mosaic-hosted-app-mobile-qr.png")
out.write_bytes(urlopen(qr_url, timeout=20).read())
print(out)
print(url)
PY
```

Phone/tablet notes:

- Verify the LAN URL from your Mac first with `curl -k https://YOUR_LAN_IP:8081/health`.
- Open `https://YOUR_LAN_IP:8081/` in Safari once before relying on the QR code, so you
  can accept the local certificate warning.
- If the phone can reach the page but live mode disconnects quickly, check the server log
  separately from the basic LAN/HTTPS setup. A page load proves the LAN path is working.
