# Local Live App

`local_live` is the blog-reader-friendly live API server.

It does not serve the LensMosaic UI. Readers should open the hosted app and point its
live backend at this service with the `backend` query parameter.

`local_live` keeps the ADK live logic local while delegating search to a hosted service
via `SEARCH_SERVICE_URL`.

The current workflow is focused on desktop browser testing with `http://127.0.0.1:8000`.
