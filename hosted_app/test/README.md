# Hosted App Load Testing

This directory contains the hosted app load-test driver, result artifacts, and
test-planning notes.

Files:

- `load_test.py`: local load-test driver for the hosted app test endpoints
- `results/`: local run outputs

## Summary

These results are from local load tests against the hosted app test endpoints in
this repository, not Cloud Run service metrics. They are still useful for sizing
the app and understanding where the current bottlenecks are.

## Goal

Measure how the hosted app behaves under concurrent usage and verify that session
state does not get mixed across users.

This testing work focuses on:

- CPU and memory usage of the hosted app
- response latency for the similar-search loop
- response latency for `find_items`
- behavior at rising concurrency levels
- consistency of responses across different sessions

## Current Architecture Notes

These details affect how the tests should be interpreted:

- Similar-item search is driven by camera image messages and published back
  through the tile websocket.
- Similar search now uses a process-wide queue with a configurable worker pool.
- `find_items` runs synchronously in-process and publishes recommended items
  back to the session tile socket.
- Session state is stored in-process and keyed by `session_id`.

Relevant code:

- [`hosted_app/app/main.py`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/app/main.py)
- [`hosted_app/app/static/js/app.js`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/app/static/js/app.js)

## Test Objectives

1. Measure end-to-end latency for the similar-search loop under concurrency.
2. Measure end-to-end latency for `find_items` under concurrency.
3. Measure server CPU and memory usage during each run.
4. Detect request failures, timeouts, disconnects, and degraded throughput.
5. Verify that one session never receives another session's similar or
   recommended results.

## Similar Search

- The original single-worker similar-search loop did not scale well. At `20`
  concurrent users it timed out heavily.
- Moving similar search to a worker pool was the biggest architectural win in
  this session.
- With `4` similar-search workers, `20` users completed cleanly with
  `p50=5.21s`, `p95=10.36s`, `0` timeouts, `0` session mismatches`.
- With `10` similar-search workers, `50` users completed cleanly with
  `p50=5.03s`, `p95=9.47s`, `0` timeouts, `0` session mismatches`.
- With `100` similar-search workers and no app-side embedding cap, `200` users
  over `30s` completed `892` updates but saw `162` timeouts and upstream
  `429 RESOURCE_EXHAUSTED` errors from Gemini Embedding 2.
- With `100` similar-search workers and
  `LENS_MOSAIC_GEMINI_EMBEDDING_MAX_RPM=1500`, the same `200`-user / `30s` test
  completed `1130` updates with `p50=2.13s`, `p95=5.92s`, `0` timeouts, and
  `0` session mismatches.

Reference results:

- [`similar-20-post-pool.json`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/results/similar-20-post-pool.json)
- [`similar-50-10-workers.json`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/results/similar-50-10-workers.json)
- [`similar-200-100-workers-30s.json`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/results/similar-200-100-workers-30s.json)
- [`similar-200-100-workers-rpm-guard-1500.json`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/results/similar-200-100-workers-rpm-guard-1500.json)

## find_items

- `find_items` was consistently more stable than the camera-driven similar-search
  path in local testing.
- At `20` concurrent users, `find_items` completed cleanly with `p50=2.26s`,
  `p95=3.04s`, `0` timeouts, and `0` session mismatches.

Reference result:

- [`find-items-20.json`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/test/results/find-items-20.json)

## Session Consistency

- In all successful unique-session runs, the tests observed `0` session
  mismatches and `0` unexpected updates.
- The current service still stores session state in memory, so this consistency
  result assumes a single app instance.

## Workloads

### Similar Search

Each virtual user:

1. Opens a tile websocket connection to `/ws_image_tile/{session_id}`.
2. Sends image inputs that match the browser UI payload shape.
3. Uses `forwardToAgent=false` or the `/test/similar` harness so the test
   isolates similar-search behavior rather than live-agent variability.
4. Measures the time from image submit to receipt of a `kind="similar"` tile
   update.

Notes:

- The worker model is latest-frame-wins, not process-every-frame.
- Under load, some intermediate frames may be skipped by design.
- The main success condition is timely delivery of the latest relevant result per
  user, not strict one-result-per-frame behavior.

### find_items

`find_items` is tested through the guarded harness path rather than through
free-form voice prompting, so latency stays comparable across runs.

Each virtual user:

1. Uses a unique `user_id` and `session_id`.
2. Triggers `find_items` with a fixed query bundle and `ranking_query`.
3. Measures the time from request start to recommended-items delivery and
   function completion.

## Metrics To Collect

### Server Resource Metrics

Collect for the hosted app process or Cloud Run service:

- average CPU utilization
- peak CPU utilization
- average memory usage
- peak memory usage
- instance count
- request rate
- error rate

### Latency Metrics

For similar search:

- p50 latency
- p95 latency
- p99 latency
- max latency
- timeout count
- disconnect count

Recommended timing points:

- image message sent by client
- image received by server
- search worker start
- search worker finish
- tile update published
- tile update received by client

For `find_items`:

- p50 latency
- p95 latency
- p99 latency
- max latency
- timeout count
- error count

Recommended timing points:

- request start
- `find_items` start
- `search_text_queries_sync` finish
- recommended tile publish
- function return
- client receives recommended items

### Throughput Metrics

Record:

- completed similar-search updates per minute
- completed `find_items` runs per minute
- failures per minute

## Test Data

Use fixed and repeatable inputs.

### Similar Search Inputs

Prepare a small image set with distinct subjects, for example:

- speaker
- handbag
- sneaker
- teapot
- shirt

Assign one image profile per virtual user so results should be visually and
semantically different across sessions.

### find_items Inputs

Prepare fixed query bundles such as:

- `queries=["red handbag","small red purse"]`
  `ranking_query="small red handbag for daily use"`
- `queries=["bookshelf speaker","compact speaker"]`
  `ranking_query="compact speaker for a small room"`
- `queries=["white teapot","ceramic tea pot"]`
  `ranking_query="simple white teapot for daily tea"`

Each user should be assigned one stable bundle during a run.

## Session Consistency Checks

### Positive Isolation Checks

For all normal runs:

- every virtual user must use a unique `user_id`
- every virtual user must use a unique `session_id`
- every virtual user must connect its own tile socket
- every virtual user must use a distinct image or query profile

Record for each virtual user:

- sent input identity
- expected profile identity
- every `similar` update received
- every `recommended` update received

Mark a failure if:

- a user receives items matching another user's test profile
- a user receives updates when it has not sent any workload input
- a tile socket receives a response tagged to the wrong session in logs
- two active users show cross-over in recommended or similar result streams

### Idle User Check

Include a small number of connected but idle users in at least one run.

Expected result:

- idle sessions receive no `similar` updates
- idle sessions receive no `recommended` updates

### Negative Collision Check

Run one non-production validation where two virtual users intentionally reuse the
same `session_id`.

Purpose:

- confirm whether session collisions produce mixed state
- document current behavior clearly

Do not mix this negative test into the main performance runs.

## Learnings

- The main scalability problem was the similar-search execution model, not
  `find_items`.
- CPU and memory were not the first limiting factors in local testing. Embedding
  throughput and quota pressure were more important.
- A request-per-minute cap on Gemini Embedding 2 is necessary to avoid upstream
  quota spikes under high camera concurrency.
- The current app-side cap is request-count based, while the upstream Vertex AI
  quota is token based. Treat `1500 RPM` as an empirically safer operating
  point, not a guaranteed universal quota match.
- Horizontal scaling is still unsafe for live sessions until session state and
  websocket fan-out move out of process.

## Cloud Run Implications

- Prefer `concurrency=500` on a single warm instance over horizontal scaling
  right now.
- Keep `max-instances=1` until session state and websocket fan-out move out of
  process.

## Instrumentation Requirements

Before load testing, add or enable structured logging with:

- `test_run_id`
- `workload`
- `user_id`
- `session_id`
- `request_id`
- event type
- start timestamp
- end timestamp
- result count
- error status

Add server-side timing logs around:

- `_collection_search`
- `_search_worker_loop`
- `search_text_queries_sync`
- `find_items`
- tile publish events

The app already logs search latency details inside `_collection_search`; extend
that logging so runs can be correlated to specific users and sessions.

## Execution Steps

1. Confirm the hosted app is reachable and healthy.
2. Confirm all virtual users generate unique `user_id` and `session_id` values.
3. Run the 1-user baseline for similar search.
4. Run the 1-user baseline for `find_items`.
5. Review logs and metrics to confirm instrumentation is working.
6. Run the 10-user similar-search test.
7. Run the 20-user similar-search test.
8. Run the 10-user `find_items` test.
9. Run the 20-user `find_items` test.
10. Run the idle-user isolation check.
11. Run the deliberate shared-session negative test.
12. Optionally run mixed-workload scenarios.

## Pass/Fail Criteria

At minimum, each run should report:

- CPU average and peak
- memory average and peak
- p50, p95, p99, and max latency
- throughput
- error count
- timeout count
- disconnect count
- session-mixing incidents

A run fails the consistency check if any session receives another session's
results during a unique-session test.

## Output Report Format

Produce one short report per scenario with:

- scenario name
- user count
- workload type
- test duration
- CPU avg and peak
- memory avg and peak
- p50, p95, p99, max latency
- throughput
- error and timeout totals
- disconnect totals
- session consistency result
- notable observations

## Recommended Next Implementation Tasks

To execute this plan reliably, the next engineering tasks should be:

1. Add structured timing and session-aware logs.
2. Add a test harness for `find_items`.
3. Build a load generator for the live websocket and tile websocket flows.
4. Add result-validation logic that flags cross-session leakage.
5. Run baseline tests before scaling to 10 and 20 users.
