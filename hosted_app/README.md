# Hosted App

`hosted_app` is the Cloud Run service for LensMosaic.

It serves:

- static UI assets
- public search endpoints
- item detail endpoints for the UI
- hosted live WebSocket endpoints for the quick demo

This app supports both:

1. same-origin hosted demo mode
2. hosted UI plus local live backend mode

This README covers two complete workflows for the hosted app:

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

If you use Gemini API mode, set `GEMINI_API_KEY` or `GOOGLE_API_KEY`.

Leave `AGENT_MODEL` unset unless you want to pin a specific live model manually.
When it is unset, the hosted app chooses a provider-appropriate default model based
on `GOOGLE_GENAI_USE_VERTEXAI`.

### 2. Run the direct model preflight

Before starting `uvicorn` or Cloud Run, verify that the live model itself is
responding from this machine:

```bash
uv run --project hosted_app python hosted_app/model_test.py --timeout 60
```

This probe runs:

- a Vertex AI text test
- a Vertex AI audio test
- a Gemini API text test, if `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set
- a Gemini API audio test, if `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set

Use this step to separate model/provider latency from app/server latency before
debugging the app server or browser behavior.

## Local testing

### 1. Quick local desktop test

Use this flow when you want to run `hosted_app` on your own machine and open it from
the same computer.

Start the app locally:

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app \
  --host 127.0.0.1 \
  --port 8081
```

Validate it locally:

```bash
curl http://127.0.0.1:8081/health
```

Open:

```text
http://127.0.0.1:8081/
```

This is the fastest loop for testing the UI, transcript behavior, item detail calls,
and search endpoints from a desktop browser.

### 2. Local HTTPS phone test on your LAN

Use this flow when you want to run `hosted_app` locally and open it from a phone or
tablet on the same LAN.

If port `8081` is already in use, stop the existing process first:

```bash
lsof -nP -iTCP:8081 -sTCP:LISTEN
kill <PID>
```

The repository includes an OpenSSL config template at
`hosted_app/app/certs/openssl-san.cnf`.

Before generating the cert, edit that file and replace the sample LAN IP with your
computer's actual LAN IP address.

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
  --port 8081 \
  --ssl-keyfile certs/lan-key.pem \
  --ssl-certfile certs/lan-cert.pem
```

Verify from your Mac before moving to the phone:

```bash
curl -k https://YOUR_LAN_IP:8081/health
```

Open this from your phone:

```text
https://YOUR_LAN_IP:8081/
```

Open the URL directly in Safari once before relying on the QR code, so you can accept
the local certificate warning explicitly.

Generate a QR code for the local LAN URL:

```bash
uv run --with 'qrcode[pil]' python - <<'PY'
from pathlib import Path
import qrcode

url = "https://YOUR_LAN_IP:8081/"
out = Path("/tmp/lens-mosaic-hosted-app-mobile-qr.png")
img = qrcode.make(url)
img.save(out)
print(out)
print(url)
PY
```

Open `/tmp/lens-mosaic-hosted-app-mobile-qr.png`, show it on your desktop, and let
the user scan it from their smartphone.

### 3. Local testing checklist

For a same-machine desktop test:

- `curl http://127.0.0.1:8081/health` succeeds
- the root URL serves the HTML app
- text chat works
- the transcript panel updates correctly
- item details open from the mosaic

For a LAN phone test:

- `curl -k https://YOUR_LAN_IP:8081/health` succeeds from your Mac
- the phone can open the local HTTPS URL
- the certificate warning can be accepted once in Safari
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
  --cpu 2 \
  --memory 2Gi \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI}",GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}",GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}",VECTOR_COLLECTION_ID="${VECTOR_COLLECTION_ID}",VECTOR_FIELD="${VECTOR_FIELD}",EMBEDDING_MODEL="${EMBEDDING_MODEL}",RANKING_CONFIG="${RANKING_CONFIG}",SEARCH_TOP_K="${SEARCH_TOP_K}"
```

If you are deploying in Gemini API mode, include whichever API key variable you use
in the service env:

```bash
gcloud run deploy lens-mosaic \
  --source hosted_app \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --allow-unauthenticated \
  --cpu 2 \
  --memory 2Gi \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI}",GEMINI_API_KEY="${GEMINI_API_KEY}",GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}",GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}",VECTOR_COLLECTION_ID="${VECTOR_COLLECTION_ID}",VECTOR_FIELD="${VECTOR_FIELD}",EMBEDDING_MODEL="${EMBEDDING_MODEL}",RANKING_CONFIG="${RANKING_CONFIG}",SEARCH_TOP_K="${SEARCH_TOP_K}"
```

If you use `GOOGLE_API_KEY` instead of `GEMINI_API_KEY`, swap that variable into the
same `--set-env-vars` list.

If you want to override the provider-specific default live model, also add
`AGENT_MODEL="${AGENT_MODEL}"` to `--set-env-vars`.

Recommended runtime settings:

- use `2 vCPU` with `2 GiB` memory for the hosted deployment
- keep `min-instances=1` so the demo stays warm
- keep `max-instances=1` for now because live session state is in memory
- use a service account with access to Vertex AI, Vector Search 2.0, and Discovery
  Engine Ranking

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

Open the app:

```text
https://YOUR_SERVICE_URL/
```

For the blog-reader mode, open:

```text
https://YOUR_SERVICE_URL/?backend=http://127.0.0.1:8000
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
- For the quickest local iteration, prefer `uvicorn` on `127.0.0.1:8081` before moving
  to LAN or Cloud Run testing.
- If the phone can reach the page but live mode disconnects quickly, check the server
  log separately from the basic LAN/HTTPS setup. A page load proves the LAN path is
  working.
- For the hosted UI plus `local_live` workflow, see
  [docs/local-reader-quickstart.md](/Users/kaz/Documents/GitHub/lens-mosaic/docs/local-reader-quickstart.md).
