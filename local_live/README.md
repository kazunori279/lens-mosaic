# Local Live App

`local_live` is a legacy standalone live API server.

The active app now uses `hosted_app` as a single-origin server for UI, search, item
details, and live WebSockets, so this service is no longer part of the default
workflow.

`local_live` keeps the ADK live logic local while delegating search to a hosted service
via `SEARCH_SERVICE_URL`.

If you still experiment with `local_live`, the standalone server runs on
`http://127.0.0.1:8000`.

For local desktop testing, prefer Gemini API mode so the server picks the
provider-specific default automatically. If local Vertex AI live audio
is slow but the same deployment is fast on Cloud Run, verify with
`hosted_app/model_test.py` first and treat that as a provider-path issue rather than a
UI bug.
