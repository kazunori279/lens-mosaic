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

- `GOOGLE_GENAI_USE_VERTEXAI=FALSE` for the fastest local desktop loop when you have a
  `GOOGLE_API_KEY`
- `GOOGLE_GENAI_USE_VERTEXAI=TRUE` only when you specifically want to test the local
  Vertex AI live path
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `SEARCH_SERVICE_URL` to your hosted Cloud Run URL

This local server only handles the live ADK path. Search and item data come from `SEARCH_SERVICE_URL`.

Before you debug the app server, run the direct model probe once:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --project hosted_app python hosted_app/model_test.py --timeout 60
```

If that probe shows:

- fast text but very slow or timed-out Vertex audio from your laptop
- smooth behavior after deploying the same app to Cloud Run

then the bottleneck is the local machine's direct Vertex live connection rather than
the browser UI or FastAPI app. In that case, use Gemini API for local desktop testing
or keep the live backend deployed on Cloud Run while you work on the UI.

## 2. Start the local live server

Desktop HTTP:

```bash
cd local_live/app
uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8000
```

## 3. Open the hosted UI

Desktop localhost example:

```text
https://YOUR_SERVICE_URL/?backend=http://127.0.0.1:8000
```

Notes:

- `127.0.0.1` only works when the browser runs on the same machine as the local live server.
- The hosted UI keeps using the hosted search API even when `backend=` points to your local server.
- The current local-live workflow is intended for desktop browser testing.
- If you want to run the hosted app locally instead of Cloud Run, start `hosted_app` on `127.0.0.1:8081` and open `http://127.0.0.1:8081/?backend=http://127.0.0.1:8000`.

## 4. Quick checks

- `http://127.0.0.1:8000/health`
- `https://YOUR_SERVICE_URL/health`
- `curl -X POST http://127.0.0.1:8081/search -H 'content-type: application/json' -d '{"text":"speaker"}'`

## 5. Expected behavior

- Similar items should appear from camera frames even before you speak.
- If you ask the agent to find matching items, the agent should speak after the `find_items(...)` tool call and update the recommendation tiles.

If the UI loads but Start never enables, check the live WebSocket backend URL first.
If transcription appears but the agent does not answer, check the local live server log for Gemini Live or tool-call errors.
