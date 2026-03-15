# Blog Sample

`blog_sample` is a minimal local FastAPI sample for blog readers.

It is intentionally smaller than `hosted_app` and focuses on:

- a local ADK live server
- bidi streaming over `/ws/{user_id}/{session_id}`
- the existing hosted UI and catalog routes, proxied from Cloud Run
- recommended-item tile updates from `find_items(...)`

It does **not** implement the full hosted app stack locally.

Current differences from `hosted_app`:

- static assets, `/search`, and `/api/item/{item_id}` are proxied to the deployed hosted app
- camera-driven similar-item search is not implemented locally
- the tile websocket is used only for recommended items from `find_items(...)`

## Files

- `app/main.py`: minimal sample server
- `app/.env`: local sample configuration

## Run locally

From the repository root:

```bash
export UV_CACHE_DIR=/tmp/uv-cache
uv run --project hosted_app uvicorn blog_sample.app.main:app --host 127.0.0.1 --port 8080
```

Open:

```text
http://127.0.0.1:8080/
```

## Environment

`blog_sample/app/.env` is the source of truth for the sample configuration.

Important values:

- `GOOGLE_CLOUD_PROJECT`: Vertex AI project for local live testing

These values are currently hardcoded in `blog_sample/app/main.py`:

- `GOOGLE_CLOUD_LOCATION=us-central1`
- `LENS_MOSAIC_COLLECTION_ID=mercari3m-collection-mm2`
- `LENS_MOSAIC_HOSTED_URL=https://lens-mosaic-nhhfh7g7iq-uc.a.run.app`

The proxied `/search` endpoint now expects:

- `queries`: a short list of English product-search queries
- `user_intent`: the user goal used for final reranking

## Use case

Use `blog_sample` when you want a compact, easier-to-read example for a write-up or tutorial.
Use `hosted_app` when you want the full local or deployed LensMosaic application.
