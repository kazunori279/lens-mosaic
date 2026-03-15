# Hosted App Load Test Plan

## Goal

Measure how the hosted app behaves under concurrent usage and verify that session
state does not get mixed across users.

This plan focuses on:

- CPU and memory usage of the hosted app
- response latency for the similar search loop
- response latency for `find_items`
- behavior at 10 concurrent users
- behavior at 20 concurrent users
- consistency of responses across different sessions

## Current Architecture Notes

These details affect how the test should be designed:

- Similar-item search is driven by camera image messages over the live WebSocket.
- Similar-item results are published back to the client through the tile WebSocket.
- Similar search uses a single process-wide queue and a single background worker
  thread.
- `find_items` runs synchronously in-process and publishes recommended items back
  to the session tile socket.
- Session state is stored in-process and keyed by `session_id`.

Relevant code:

- [`hosted_app/app/main.py`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/app/main.py)
- [`hosted_app/app/static/js/app.js`](/Users/kaz/Documents/GitHub/lens-mosaic/hosted_app/app/static/js/app.js)

## Test Objectives

1. Measure end-to-end latency for the similar search loop at 10 and 20 concurrent users.
2. Measure end-to-end latency for `find_items` at 10 and 20 concurrent users.
3. Measure server CPU and memory usage during each run.
4. Detect request failures, timeouts, disconnects, and degraded throughput.
5. Verify that one session never receives another session's similar or recommended
   results.

## Workloads

### Workload A: Similar Search Loop

Each virtual user should:

1. Open a live WebSocket connection to `/ws/{user_id}/{session_id}`.
2. Open a tile WebSocket connection to `/ws_image_tile/{session_id}`.
3. Send image messages in the same shape as the browser UI.
4. Set `forwardToAgent=false` so the test isolates similar search behavior rather
   than live vision model behavior.
5. Measure the time from image send to receipt of a `kind="similar"` tile update.

Notes:

- The current worker model is latest-frame-wins, not process-every-frame.
- Under load, some intermediate frames may be skipped by design.
- The primary success condition is timely delivery of the latest relevant result
  per user, not strict one-result-per-frame behavior.

### Workload B: `find_items`

`find_items` is not a public HTTP route. To test it consistently, use one of the
following approaches:

1. Preferred: add a test-only harness that directly invokes the same server-side
   code path as `find_items`.
2. Acceptable fallback: add a guarded internal test endpoint that calls the same
   function with fixed query sets.

Do not rely on free-form voice prompting for the benchmark because that adds model
variability and makes latency comparisons noisy.

For each virtual user:

1. Use a unique `user_id` and `session_id`.
2. Trigger `find_items` with a fixed set of queries and a fixed `ranking_query`.
3. Measure the time from request start to recommended-items delivery and function
   completion.

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

If the app is running on Cloud Run, collect these from Cloud Monitoring for the
same wall-clock window as each load test.

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

### Similar Search Test Inputs

Prepare a small image set with distinct subjects, for example:

- speaker
- handbag
- sneaker
- teapot
- shirt

Assign one image profile per virtual user so results should be visually and
semantically different across sessions.

### `find_items` Test Inputs

Prepare fixed query bundles such as:

- `queries=["red handbag","small red purse"]`
  `ranking_query="small red handbag for daily use"`
- `queries=["bookshelf speaker","compact speaker"]`
  `ranking_query="compact speaker for a small room"`
- `queries=["white teapot","ceramic tea pot"]`
  `ranking_query="simple white teapot for daily tea"`

Each user should be assigned one stable bundle during a run.

## Concurrency Matrix

Run each workload separately first.

### Baseline

- 1 concurrent user for similar search
- 1 concurrent user for `find_items`

### Target Runs

- 10 concurrent users for similar search
- 20 concurrent users for similar search
- 10 concurrent users for `find_items`
- 20 concurrent users for `find_items`

### Optional Mixed Runs

After the isolated runs are stable, add:

- 10 concurrent users total, split evenly across the two workloads
- 20 concurrent users total, split evenly across the two workloads

## Traffic Shape

To keep runs comparable:

- ramp up over 60 to 120 seconds
- hold steady for 10 minutes
- cool down for 60 seconds

Suggested per-user pacing:

- Similar search: send one image every 2 to 3 seconds
- `find_items`: trigger one search every 3 to 5 seconds

Avoid burst-only tests at first. A steady-state test is more useful for capacity
planning and session-consistency checks.

## Session Consistency Checks

This is a required part of the plan.

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
3. Build a load generator for the live WebSocket and tile WebSocket flows.
4. Add result-validation logic that flags cross-session leakage.
5. Run baseline tests before scaling to 10 and 20 users.
