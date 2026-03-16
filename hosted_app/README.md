# Hosted App

`hosted_app` is the Cloud Run service for LensMosaic.

If you want a much smaller local example for documentation or tutorials, see
[`blog_sample/README.md`](../blog_sample/README.md).

It serves:

- static UI assets
- public search endpoints
- item detail endpoints for the UI
- hosted live WebSocket endpoints for the quick demo

This app now uses a single-origin architecture for:

- static UI
- search and item detail APIs
- live WebSocket sessions

This README covers two deployment environments for the hosted app:

- local testing on your machine or LAN
- remote deployment to Cloud Run

Use the local flow when you are debugging the UI, live session behavior, or phone
permissions before touching Cloud Run. Use the remote flow when you want the hosted
demo available from a public URL.

## Shared setup

### 1. Configure the app

Create a local env file:

```bash
cd hosted_app/app
cp .env.example .env
```

Set the required values in `.env` for your environment.

`GOOGLE_GENAI_USE_VERTEXAI` controls the live model backend:

- `TRUE`: use Vertex AI live mode
- `FALSE`: use Gemini API live mode

If you use Gemini API mode, set `GOOGLE_API_KEY`.

Set `LENS_MOSAIC_COLLECTION_ID=mercari3m-collection-mm2`.
The hosted app now supports only the Gemini Embedding collection and derives the
vector fields from that collection ID.

Set `LENS_MOSAIC_SIMILAR_SEARCH_WORKERS` to control the number of background
workers used for camera-driven similar search. The default is `10`.

Set `LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM` to cap the total number of Gemini
Embedding 2 requests the app will issue in a rolling 60-second window. The
default is `1500`. When the app is over that budget, session-backed UI requests
reuse the last published results instead of sending a new embedding request.

If you run these commands in Codex or another sandboxed environment, set:

```bash
export UV_CACHE_DIR=/tmp/uv-cache
```

That prevents `uv` from failing when it cannot write to its default cache
directory.

Supported dataset:

### mercari3m-collection-mm2 (Gemini Embedding)

- **Collection**: `mercari3m-collection-mm2`
- **ANN Indexes**: `text-emb-index` (text), `image-emb-index` (image)
- **Dataset ID**: `mercari3m_mm2`
- **Embedding Model**: `gemini-embedding-2-preview` (BYOE - Bring Your Own Embeddings)
- **Embedding Dimensions**: `768` (reduced from `3072` default)
- **Vector Fields**: `text_emb` (from `{name} {description}`), `image_emb` (from product image)
- **Distance Metric**: `DOT_PRODUCT`
- **Data Objects**: `882,688` items

### 2. Run the direct model preflight

Before starting `uvicorn` or Cloud Run, verify that the live model itself is
responding from this machine:

```bash
uv run --project hosted_app python hosted_app/model_test.py --timeout 60
```

This probe runs:

- a Vertex AI text test
- a Vertex AI audio test
- a Gemini API text test, if `GOOGLE_API_KEY` is set
- a Gemini API audio test, if `GOOGLE_API_KEY` is set

Use this step to separate model/provider latency from app/server latency before
debugging the app server or browser behavior.

## Local testing

### 1. Local HTTPS test on your LAN

When you run `hosted_app` locally, serve it over HTTPS on your LAN so the same server
is reachable from both your desktop browser and smartphones on the same network. Use
this as the default local workflow instead of a localhost-only server.

If port `8080` is already in use, stop the existing process first:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
kill <PID>
```

Determine your Mac's LAN IP once and reuse it for the cert, health check, browser
URL, and QR code:

```bash
export LENS_MOSAIC_LAN_IP="$(ipconfig getifaddr en0 || ipconfig getifaddr en1)"
export LENS_MOSAIC_URL="https://${LENS_MOSAIC_LAN_IP}:8080/"
printf '%s\n' "$LENS_MOSAIC_URL"
```

If `LENS_MOSAIC_LAN_IP` is empty, make sure your Mac is connected to Wi-Fi and rerun
the command with the correct network interface.

The repository includes an OpenSSL config template at
`hosted_app/app/certs/openssl-san.cnf`.

Before generating the cert, update that file with your current LAN IP address:

```bash
cd hosted_app/app
python - <<'PY'
from pathlib import Path
import os
import re

ip = os.environ["LENS_MOSAIC_LAN_IP"]
path = Path("certs/openssl-san.cnf")
text = path.read_text()
text = re.sub(r"^CN = .+$", f"CN = {ip}", text, flags=re.MULTILINE)
text = re.sub(r"^IP\\.2 = .+$", f"IP.2 = {ip}", text, flags=re.MULTILINE)
path.write_text(text)
print(f"Updated {path} for {ip}")
PY
```

Generate local cert files:

```bash
cd hosted_app/app
mkdir -p certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/lan-key.pem \
  -out certs/lan-cert.pem \
  -config certs/openssl-san.cnf \
  -extensions req_ext
```

These generated cert files are ignored by git and should stay local to your machine.

Start the hosted app over HTTPS:

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --ssl-keyfile certs/lan-key.pem \
  --ssl-certfile certs/lan-cert.pem
```

Verify from your Mac before moving to the phone:

```bash
curl -k "${LENS_MOSAIC_URL}health"
```

Run a basic text search check from your Mac:

```bash
curl -k -X POST "${LENS_MOSAIC_URL}search" \
  -H 'Content-Type: application/json' \
  -d '{"queries":["red handbag","small red purse"],"ranking_query":"small red handbag for daily use"}'
```

Current local image-search latency on `mercari3m-collection-mm2` is roughly:

- warm steady-state total: `1.1s` to `1.35s`
- embedding generation: about `0.6s` to `0.9s`
- text vector search: about `0.23s`
- image vector search: about `0.22s`
- local RRF fusion: negligible

The first request after startup can be slower, around `1.5s` to `3.6s`.

Open this from your desktop browser or phone:

```text
${LENS_MOSAIC_URL}
```

Open the URL directly in Safari once before relying on the QR code, so you can accept
the local certificate warning explicitly.

Generate a QR code for the local LAN URL:

```bash
uv run --with 'qrcode[pil]' python - <<'PY'
from pathlib import Path
import os
import qrcode

url = os.environ["LENS_MOSAIC_URL"]
out = Path("/tmp/lens-mosaic-hosted-app-mobile-qr.png")
img = qrcode.make(url)
img.save(out)
print(out)
print(url)
PY
```

Open `/tmp/lens-mosaic-hosted-app-mobile-qr.png`, show it on your desktop, and let
the user scan it from their smartphone.

### 2. Local testing checklist

- `curl -k https://YOUR_LAN_IP:8080/health` succeeds from your Mac
- `curl -k -X POST https://YOUR_LAN_IP:8080/search ...` returns reranked search results
- the root URL serves the HTML app on your desktop browser
- the phone can open the local HTTPS URL
- the certificate warning can be accepted once in Safari
- text chat works
- the transcript panel updates correctly
- item details open from the mosaic
- camera permission works
- microphone permission works
- the live connection starts and stays connected

## Remote deployment

### 1. Deploy to Cloud Run

Use the values in `hosted_app/app/.env` as the source of truth for the deployment.

From the repository root, export the values you want to deploy:

```bash
set -a
source hosted_app/app/.env
set +a
```

Deploy from the repository root:

```bash
gcloud run deploy lens-mosaic \
  --source hosted_app \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --allow-unauthenticated \
  --concurrency 500 \
  --cpu 2 \
  --memory 2Gi \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI}",GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}",GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}",LENS_MOSAIC_COLLECTION_ID="${LENS_MOSAIC_COLLECTION_ID}",LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM="${LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM}"
```

If you are deploying in Gemini API mode, include whichever API key variable you use
in the service env:

```bash
gcloud run deploy lens-mosaic \
  --source hosted_app \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --allow-unauthenticated \
  --concurrency 500 \
  --cpu 2 \
  --memory 2Gi \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI}",GOOGLE_API_KEY="${GOOGLE_API_KEY}",GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}",GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}",LENS_MOSAIC_COLLECTION_ID="${LENS_MOSAIC_COLLECTION_ID}",LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM="${LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM}"
```

Older env files may still use `GEMINI_API_KEY`, but `GOOGLE_API_KEY` is the primary
variable going forward.

Recommended runtime settings:

- use `2 vCPU` with `2 GiB` memory for the hosted deployment
- use `concurrency=500` so one warm instance can hold many websocket sessions
- keep `min-instances=1` so the demo stays warm
- keep `max-instances=1` for now because live session state and tile routing are
  still in memory
- use a service account with access to Vertex AI, Vector Search 2.0, and Discovery
  Engine Ranking

For the detailed local load-test plan, results, and notes from this session, see
[`hosted_app/test/README.md`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/README.md).

### 2. Make the service public if needed

In some projects, `gcloud run deploy --allow-unauthenticated` still finishes with an
IAM warning instead of granting public access.

If that happens, run:

```bash
gcloud run services add-iam-policy-binding lens-mosaic \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --member=allUsers \
  --role=roles/run.invoker
```

Your account needs `run.services.setIamPolicy` on the service to do this.

### 3. Validate the deployed service

Get the service URL:

```bash
gcloud run services describe lens-mosaic \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --format='value(status.url)'
```

Check health with a `GET` request:

```bash
curl "$(gcloud run services describe lens-mosaic --project "${GOOGLE_CLOUD_PROJECT}" --region "${GOOGLE_CLOUD_LOCATION}" --format='value(status.url)')/health"
```

Check search with a text query:

```bash
curl -X POST "$(gcloud run services describe lens-mosaic --project "${GOOGLE_CLOUD_PROJECT}" --region "${GOOGLE_CLOUD_LOCATION}" --format='value(status.url)')/search" \
  -H 'Content-Type: application/json' \
  -d '{"queries":["red handbag","small red purse"],"ranking_query":"small red handbag for daily use"}'
```

Open the app:

```text
https://YOUR_SERVICE_URL/
```

Note: `curl -I` sends `HEAD`, and the app currently responds with `405` on routes
that only allow `GET`. Use a normal `GET` request when validating reachability.

### 4. Generate a QR code for the hosted URL

After the Cloud Run service is reachable, you can generate a QR code for the hosted
URL:

```bash
uv run --with 'qrcode[pil]' python - <<'PY'
from pathlib import Path
import qrcode

url = "https://YOUR_SERVICE_URL/"
out = Path("/tmp/lens-mosaic-cloud-run-qr.png")
img = qrcode.make(url)
img.save(out)
print(out)
print(url)
PY
```

Open `/tmp/lens-mosaic-cloud-run-qr.png` on your desktop and let the user scan it
from their phone.

### 5. Remote deployment checklist

For a Cloud Run deployment:

- `GET /health` returns `status: ok`
- the root URL serves the HTML app
- the public URL opens from a desktop browser
- the QR code opens the hosted app from a phone
- camera permission works
- microphone permission works
- the live connection starts and stays connected

## Notes

- If `model_test.py` is already slow, fix the provider/model issue before debugging the
  hosted app server.
- If `model_test.py` shows fast local text turns but slow or timed-out local Vertex AI
  audio turns, while the same app is smooth on Cloud Run, treat that as a
  machine-to-Vertex live path issue rather than a FastAPI/UI regression. For local
  desktop work, prefer Gemini API mode.
- For local iteration, prefer the LAN HTTPS flow so the same server stays reachable
  from both your desktop browser and your phone.
- If the phone can reach the page but live mode disconnects quickly, check the server
  log separately from the basic LAN/HTTPS setup. A page load proves the LAN path is
  working.
- For a localhost-only development loop, see
  [docs/local-reader-quickstart.md](../docs/local-reader-quickstart.md).
