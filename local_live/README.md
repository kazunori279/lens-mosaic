# Local Live App

`local_live` is the blog-reader-friendly live API server.

It keeps the ADK live logic local while delegating search to a hosted service via `SEARCH_SERVICE_URL`.

The intended reader flow is:

1. start `local_live/app/main.py`
2. open the hosted UI
3. pass `?backend=http://127.0.0.1:8000` or a LAN URL to the hosted UI
