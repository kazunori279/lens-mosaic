# Local Reader Quickstart

This local workflow now uses a single server:

- local hosted UI
- local search API
- local live API server

## 1. Configure the hosted app

```bash
cd hosted_app/app
cp .env.example .env
```

Set:

- `GOOGLE_GENAI_USE_VERTEXAI=FALSE` for the fastest local desktop loop when you have a
  `GOOGLE_API_KEY`
- `GOOGLE_GENAI_USE_VERTEXAI=TRUE` only when you specifically want to test the local
  Vertex AI live path
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `LENS_MOSAIC_COLLECTION_ID`

Before you debug the app server, run the direct model probe once:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --project hosted_app python hosted_app/model_test.py --timeout 60
```

If that probe shows:

- fast text but very slow or timed-out Vertex audio from your laptop
- smooth behavior after deploying the same app to Cloud Run

then the bottleneck is the local machine's direct Vertex live connection rather than
the browser UI or FastAPI app. In that case, use Gemini API for local desktop testing
before debugging layout or browser behavior.

## 2. Start the local app server

Desktop HTTP:

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app --host 127.0.0.1 --port 8081
```

## 3. Open the app

Desktop localhost example:

```text
http://127.0.0.1:8081/
```

Notes:

- `127.0.0.1` only works when the browser runs on the same machine as the local app server.
- If you want phone camera access, run the hosted app over HTTPS on your LAN as described in [hosted_app/README.md](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/README.md).

## 4. Quick checks

- `http://127.0.0.1:8081/health`
- `curl -X POST http://127.0.0.1:8081/search -H 'content-type: application/json' -d '{"text":"speaker"}'`

## 5. Expected behavior

- Similar items should appear from camera frames even before you speak.
- If you ask the agent to find matching items, the agent should speak after the `find_items(...)` tool call and update the recommendation tiles.

If the UI loads but Start never enables, check the single app server first.
If transcription appears but the agent does not answer, check the hosted app log for Gemini Live or tool-call errors.
