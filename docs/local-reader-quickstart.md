# Local Reader Quickstart

This is the blog-reader flow:

- hosted UI
- hosted search API
- local live API server

## 1. Configure the local live app

```bash
cd local_live/app
cp .env.example .env
```

Set:

- `SEARCH_SERVICE_URL` to your hosted Cloud Run URL
- `AGENT_MODEL` if you want to override the default

## 2. Start the local live server

HTTP:

```bash
cd local_live/app
uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8000
```

HTTPS for phone or tablet testing:

```bash
cd local_live/app
uv run --project .. uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --ssl-keyfile certs/lan-key.pem \
  --ssl-certfile certs/lan-cert.pem
```

## 3. Open the hosted UI

Desktop localhost example:

```text
https://YOUR_SERVICE_URL/?backend=http://127.0.0.1:8000
```

LAN example for phone or tablet:

```text
https://YOUR_SERVICE_URL/?backend=https://YOUR_LAN_IP:8000
```

## 4. Quick checks

- `http://127.0.0.1:8000/health` or `https://YOUR_LAN_IP:8000/health`
- `https://YOUR_SERVICE_URL/health`

If the UI loads but Start never enables, check the live WebSocket backend URL first.
