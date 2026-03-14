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

- `GOOGLE_GENAI_USE_VERTEXAI=TRUE`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `SEARCH_SERVICE_URL` to your hosted Cloud Run URL
- `AGENT_MODEL` if you want to override the default

This local server only handles the live ADK path. Search and item data come from `SEARCH_SERVICE_URL`.

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

## 4. Quick checks

- `http://127.0.0.1:8000/health`
- `https://YOUR_SERVICE_URL/health`

## 5. Expected behavior

- Similar items should appear from camera frames even before you speak.
- If you ask the agent to find matching items, the agent should speak after the `find_items(...)` tool call and update the recommendation tiles.

If the UI loads but Start never enables, check the live WebSocket backend URL first.
If transcription appears but the agent does not answer, check the local live server log for Gemini Live or tool-call errors.
