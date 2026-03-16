# Build a Live Shopping Agent with ADK + Gemini Embedding 2 in 5 Minutes

LensMosaic is a live multimodal shopping app. You point your camera at an item to find a similar one from one million items instantly, talk to the app, and get product matches or recommendations back in the same browser session. The app supports two core flows: similar-item search from live visual input, and recommendation queries grounded in what the user is looking at and asking for.

Under the hood, LensMosaic uses FastAPI for the app server, WebSockets for live browser sessions, ADK for the agent loop, ADK Gemini Live API Toolkit for multimodal interaction, Gemini Embedding 2 for retrieval text and image vectors, Vertex AI Vector Search 2.0 for candidate lookup, and Ranking API for final reranking. The result is a single-origin app that handles UI, live session traffic, and product retrieval in one place.

The `main.py` in this post is a digested version of that app. The user experience is intentionally narrower for the sake of clarity: you still get a live voice, text, and camera conversation with product recommendations in the browser, but you do not get the full similar-item search experience. The interesting parts are the live agent loop and recommendation flow, where you can see how FastAPI and ADK Gemini Live API Toolkit work together to realize a real-time interactive UX from browser input to model events, tool calls, and product tiles on screen.

## Learn More

- [LensMosaic Live Demo](https://lens-mosaic-761793285222.us-central1.run.app/) and [GitHub repo](https://github.com/kazunori279/lens-mosaic/tree/main)
- [ADK](https://google.github.io/adk-docs/) and [ADK Gemini Live API Toolkit](https://google.github.io/adk-docs/streaming/)
- [Gemini Embedding 2](https://cloud.google.com/vertex-ai/generative-ai/docs/embeddings/get-text-embeddings)
- [Vertex AI Vector Search 2.0](https://cloud.google.com/vertex-ai/docs/vector-search-2/overview)
- [Vertex AI Ranking API](https://docs.cloud.google.com/generative-ai-app-builder/docs/ranking)
