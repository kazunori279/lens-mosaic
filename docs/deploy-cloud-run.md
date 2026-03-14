# Deploy To Cloud Run

These steps deploy the all-in-one hosted app from `hosted_app/`.

The hosted service supports both:

- full hosted demo mode
- hosted UI plus local live backend mode for blog readers

## 1. Prepare environment variables

Use `hosted_app/app/.env.example` as the source of truth for the variables you need to provide to Cloud Run.

Set values for:

- `GOOGLE_GENAI_USE_VERTEXAI`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `VECTOR_COLLECTION_ID`
- `VECTOR_FIELD`
- `EMBEDDING_MODEL`
- `SEARCH_TOP_K`
- `RANKING_CONFIG`
- `AGENT_MODEL`

## 2. Build and deploy

From the repository root:

```bash
gcloud run deploy lens-mosaic \
  --source hosted_app \
  --region us-central1 \
  --allow-unauthenticated \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1,VECTOR_COLLECTION_ID=YOUR_COLLECTION_ID,VECTOR_FIELD=embedding,RANKING_CONFIG=projects/YOUR_PROJECT_ID/locations/global/rankingConfigs/default_ranking_config,AGENT_MODEL=gemini-live-2.5-flash-native-audio
```

## 3. Recommended runtime settings

- Keep `min-instances=1` so the demo is warm.
- Keep `max-instances=1` for now because live session state is in memory.
- Use a service account with access to:
  - Vertex AI
  - Vector Search 2.0 collection access
  - Discovery Engine Ranking API

## 4. Validate

Check:

```bash
curl https://YOUR_SERVICE_URL/health
```

Then open:

```text
https://YOUR_SERVICE_URL/
```

That should exercise the full hosted demo mode.

For the blog-reader mode, open:

```text
https://YOUR_SERVICE_URL/?backend=http://127.0.0.1:8000
```

or a LAN HTTPS backend for mobile devices:

```text
https://YOUR_SERVICE_URL/?backend=https://YOUR_LAN_IP:8000
```
