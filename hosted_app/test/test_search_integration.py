from __future__ import annotations

import base64
import math
import queue
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import main as app_main


class SearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        app_main.SESSION_STATES.clear()
        app_main.MAIN_LOOP = None

    def tearDown(self) -> None:
        app_main.SESSION_STATES.clear()
        app_main.MAIN_LOOP = None

    @contextmanager
    def make_client(self):
        with (
            patch.object(app_main, "_ensure_search_workers", return_value=None),
            patch.object(app_main, "_stop_search_workers", return_value=None),
        ):
            with TestClient(app_main.app) as client:
                yield client

    def test_search_endpoint_strips_inputs_and_returns_results(self) -> None:
        search_results = [
            {
                "id": "item-1",
                "name": "Red Bag",
                "description": "Compact bag",
                "score": 0.92,
            }
        ]
        with (
            patch.object(
                app_main,
                "search_text_queries_sync",
                return_value=search_results,
            ) as search_mock,
            self.make_client() as client,
        ):
            response = client.post(
                "/search",
                json={
                    "queries": ["  red bag  ", " ", "small purse"],
                    "ranking_query": "  daily handbag  ",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), search_results)
        search_mock.assert_called_once_with(
            ["red bag", "small purse"],
            "daily handbag",
        )

    def test_search_endpoint_rejects_blank_queries(self) -> None:
        with self.make_client() as client:
            response = client.post(
                "/search",
                json={"queries": [" ", ""], "ranking_query": "speaker"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"detail": "queries must include at least one non-empty string"},
        )

    def test_search_endpoint_rejects_blank_ranking_query(self) -> None:
        with self.make_client() as client:
            response = client.post(
                "/search",
                json={"queries": ["speaker"], "ranking_query": " "},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"detail": "ranking_query must be a non-empty string"},
        )

    def test_search_endpoint_surfaces_rate_limit_as_429(self) -> None:
        with (
            patch.object(
                app_main,
                "search_text_queries_sync",
                side_effect=app_main.EmbeddingRateLimitExceeded("embedding budget hit"),
            ),
            self.make_client() as client,
        ):
            response = client.post(
                "/search",
                json={"queries": ["speaker"], "ranking_query": "speaker"},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"detail": "embedding budget hit"})

    def test_search_endpoint_hydrates_vs2_rrf_results_without_inline_data(self) -> None:
        item_id = "item-123"
        search_result = app_main.vectorsearch_v1beta.SearchResult(
            data_object=app_main.vectorsearch_v1beta.DataObject(
                name=f"{app_main._collection_path()}/dataObjects/{item_id}",
                data_object_id=item_id,
            ),
            distance=0.42,
        )
        batch_response = app_main.vectorsearch_v1beta.BatchSearchDataObjectsResponse(
            results=[
                app_main.vectorsearch_v1beta.SearchDataObjectsResponse(
                    results=[search_result]
                )
            ]
        )
        hydrated_item = {
            "id": item_id,
            "name": "Hydrated Speaker",
            "description": "Fetched from item details",
            "price": "$99",
            "url": "https://example.com/item-123",
            "img_url": "https://example.com/item-123.jpg",
        }

        with (
            patch.object(
                app_main,
                "_embed_with_gemini_embedding_2",
                return_value=[0.1, 0.2, 0.3],
            ),
            patch.object(
                app_main.search_client,
                "batch_search_data_objects",
                return_value=batch_response,
            ) as batch_search_mock,
            patch.object(
                app_main,
                "_get_item_details",
                return_value=hydrated_item,
            ) as get_item_mock,
            patch.object(
                app_main,
                "_rank_results",
                side_effect=lambda _query, results: results,
            ),
            self.make_client() as client,
        ):
            response = client.post(
                "/search",
                json={
                    "queries": ["bookshelf speaker"],
                    "ranking_query": "bookshelf speaker for a small room",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": item_id,
                    "name": "Hydrated Speaker",
                    "description": "Fetched from item details",
                    "score": 0.42,
                }
            ],
        )
        batch_search_mock.assert_called_once()
        get_item_mock.assert_called_once_with(item_id)

    def test_image_similarity_search_uses_only_image_vector_field(self) -> None:
        item_id = "item-456"
        search_result = app_main.vectorsearch_v1beta.SearchResult(
            data_object=app_main.vectorsearch_v1beta.DataObject(
                name=f"{app_main._collection_path()}/dataObjects/{item_id}",
                data_object_id=item_id,
                data={
                    "name": "Camera Bag",
                    "description": "Compact shoulder bag",
                },
            ),
            distance=0.73,
        )
        search_response = app_main.vectorsearch_v1beta.SearchDataObjectsResponse(
            results=[search_result]
        )

        with (
            patch.object(
                app_main,
                "_embed_with_gemini_embedding_2",
                return_value=[0.1, 0.2, 0.3],
            ),
            patch.object(
                app_main.search_client,
                "search_data_objects",
                return_value=search_response,
            ) as search_mock,
            patch.object(
                app_main.search_client,
                "batch_search_data_objects",
            ) as batch_search_mock,
        ):
            results = app_main._image_similarity_search(b"image-bytes")

        self.assertEqual(
            results,
            [
                {
                    "id": item_id,
                    "name": "Camera Bag",
                    "description": "Compact shoulder bag",
                    "score": 0.73,
                }
            ],
        )
        search_mock.assert_called_once()
        batch_search_mock.assert_not_called()
        request = search_mock.call_args.args[0]
        self.assertEqual(request.parent, app_main._collection_path())
        self.assertEqual(
            request.vector_search.search_field,
            app_main.ACTIVE_COLLECTION.image_vector_field,
        )
        self.assertEqual(request.vector_search.top_k, app_main.SEARCH_TOP_K)

    def test_rank_endpoint_returns_ranked_results(self) -> None:
        ranked_results = [
            {
                "id": "item-2",
                "name": "Top Pick",
                "description": "Best option",
                "score": 0.99,
            }
        ]
        request_results = [
            {
                "id": "item-1",
                "name": "Candidate",
                "description": "Candidate description",
                "score": 0.5,
            }
        ]
        with (
            patch.object(
                app_main,
                "_rank_results",
                return_value=ranked_results,
            ) as rank_mock,
            self.make_client() as client,
        ):
            response = client.post(
                "/rank",
                json={"query": "best speaker", "results": request_results},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ranked_results)
        rank_mock.assert_called_once_with("best speaker", request_results)

    def test_search_text_queries_sync_runs_each_query_in_a_thread_then_reranks(self) -> None:
        calls: list[tuple[str, str]] = []
        calls_lock = threading.Lock()
        query_results = {
            "red bag": [
                {
                    "id": "item-1",
                    "name": "Red Bag",
                    "description": "Compact bag",
                    "score": 0.9,
                },
                {
                    "id": "item-2",
                    "name": "Crossbody Bag",
                    "description": "Lightweight bag",
                    "score": 0.8,
                },
            ],
            "small purse": [
                {
                    "id": "item-2",
                    "name": "Crossbody Bag",
                    "description": "Lightweight bag",
                    "score": 0.7,
                },
                {
                    "id": "item-3",
                    "name": "Mini Purse",
                    "description": "Small evening purse",
                    "score": 0.6,
                },
            ],
        }
        reranked_results = [
            {
                "id": "item-3",
                "name": "Mini Purse",
                "description": "Small evening purse",
                "score": 0.99,
            }
        ]

        def fake_collection_search(*, text=None, image=None, rerank=True):
            self.assertIsNone(image)
            self.assertFalse(rerank)
            self.assertIsNotNone(text)
            with calls_lock:
                calls.append((threading.current_thread().name, text))
            return query_results[text]

        with (
            patch.object(
                app_main,
                "_collection_search",
                side_effect=fake_collection_search,
            ) as search_mock,
            patch.object(
                app_main,
                "_rank_results",
                return_value=reranked_results,
            ) as rank_mock,
        ):
            results = app_main.search_text_queries_sync(
                ["red bag", "small purse"],
                "daily handbag",
            )

        self.assertEqual(results, reranked_results)
        self.assertEqual(search_mock.call_count, 2)
        self.assertCountEqual(
            [query for _thread_name, query in calls],
            ["red bag", "small purse"],
        )
        self.assertTrue(all(thread_name != "MainThread" for thread_name, _query in calls))
        rank_mock.assert_called_once_with(
            "daily handbag",
            [
                {
                    "id": "item-1",
                    "name": "Red Bag",
                    "description": "Compact bag",
                    "score": 0.9,
                },
                {
                    "id": "item-2",
                    "name": "Crossbody Bag",
                    "description": "Lightweight bag",
                    "score": 0.8,
                },
                {
                    "id": "item-3",
                    "name": "Mini Purse",
                    "description": "Small evening purse",
                    "score": 0.6,
                },
            ],
        )

    def test_get_item_endpoint_returns_item_details(self) -> None:
        item = {
            "id": "item-1",
            "name": "Red Bag",
            "description": "Compact bag",
            "price": "$10",
            "url": "https://example.com/item-1",
            "img_url": "https://example.com/item-1.jpg",
        }
        with (
            patch.object(app_main, "_get_item_details", return_value=item),
            self.make_client() as client,
        ):
            response = client.get("/api/item/item-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), item)

    def test_get_item_endpoint_returns_404_for_missing_item(self) -> None:
        with (
            patch.object(app_main, "_get_item_details", return_value=None),
            self.make_client() as client,
        ):
            response = client.get("/api/item/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Item not found"})

    def test_find_items_test_endpoint_requires_flag(self) -> None:
        with (
            patch.object(app_main, "TEST_ENDPOINTS_ENABLED", False),
            self.make_client() as client,
        ):
            response = client.post(
                "/test/find_items",
                json={
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "queries": ["speaker"],
                    "ranking_query": "speaker",
                    "publish": False,
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Test endpoints are disabled"})

    def test_find_items_test_endpoint_returns_trimmed_results(self) -> None:
        items = [
            {
                "id": "item-1",
                "name": "Red Bag",
                "description": "Compact bag",
                "score": 0.92,
            },
            {
                "id": "item-2",
                "name": "Everyday Tote",
                "description": "Larger bag",
                "score": 0.88,
            },
        ]
        with (
            patch.object(app_main, "TEST_ENDPOINTS_ENABLED", True),
            patch.object(
                app_main,
                "_run_find_items_for_session",
                return_value=(items, 12.5),
            ) as run_mock,
            self.make_client() as client,
        ):
            response = client.post(
                "/test/find_items",
                json={
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "queries": ["  red bag  ", " ", "small purse"],
                    "ranking_query": "  daily handbag  ",
                    "publish": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "item_ids": ["item-1", "item-2"],
                "item_names": ["Red Bag", "Everyday Tote"],
                "latency_ms": 12.5,
            },
        )
        run_mock.assert_called_once_with(
            session_id="session-1",
            user_id="user-1",
            queries=["red bag", "small purse"],
            ranking_query="daily handbag",
            publish=False,
        )

    def test_run_find_items_for_session_reports_elapsed_latency_ms(self) -> None:
        returned_items = [
            {
                "id": f"item-{index}",
                "name": f"Item {index}",
                "description": "Candidate",
                "score": 1.0 - (index * 0.01),
            }
            for index in range(app_main.MAX_TILE_ITEMS + 5)
        ]
        with (
            patch.object(
                app_main,
                "search_text_queries_sync",
                return_value=returned_items,
            ) as search_mock,
            patch.object(
                app_main,
                "perf_counter",
                side_effect=[100.0, 100.125],
            ),
        ):
            items, latency_ms = app_main._run_find_items_for_session(
                session_id="session-1",
                user_id="user-1",
                queries=["bookshelf speaker"],
                ranking_query="bookshelf speaker for a small room",
                publish=False,
            )

        self.assertEqual(len(items), app_main.MAX_TILE_ITEMS)
        self.assertTrue(math.isclose(latency_ms, 125.0, rel_tol=0, abs_tol=0.001))
        search_mock.assert_called_once_with(
            ["bookshelf speaker"],
            "bookshelf speaker for a small room",
        )

    def test_session_state_replaces_live_websocket_without_clearing_new_connection(self) -> None:
        session = app_main.session_state_for("session-1", "user-1")
        first_ws = object()
        second_ws = object()

        previous_ws, first_connection_id = session.attach_live_client(first_ws, "user-1")
        self.assertIsNone(previous_ws)
        self.assertIs(session.live_client, first_ws)
        self.assertEqual(session.user_id, "user-1")

        previous_ws, second_connection_id = session.attach_live_client(second_ws, "user-1")
        self.assertIs(previous_ws, first_ws)
        self.assertIs(session.live_client, second_ws)
        self.assertEqual(second_connection_id, first_connection_id + 1)

        detached_current = session.detach_live_client(first_ws, first_connection_id)
        self.assertFalse(detached_current)
        self.assertIs(session.live_client, second_ws)
        self.assertEqual(session.user_id, "user-1")

        detached_current = session.detach_live_client(second_ws, second_connection_id)
        self.assertTrue(detached_current)
        self.assertIsNone(session.live_client)
        self.assertIsNone(session.user_id)

    def test_cleanup_keeps_session_state_while_live_websocket_is_active(self) -> None:
        session = app_main.session_state_for("session-1", "user-1")
        _previous_ws, connection_id = session.attach_live_client(object(), "user-1")

        app_main.cleanup("session-1", session)
        self.assertIn("session-1", app_main.SESSION_STATES)

        session.detach_live_client(session.live_client, connection_id)
        app_main.cleanup("session-1", session)
        self.assertNotIn("session-1", app_main.SESSION_STATES)

    def test_similar_test_endpoint_decodes_image_and_updates_session(self) -> None:
        image_bytes = b"fake-image"
        image_b64 = base64.b64encode(image_bytes).decode("ascii")

        with (
            patch.object(app_main, "TEST_ENDPOINTS_ENABLED", True),
            self.make_client() as client,
        ):
            response = client.post(
                "/test/similar",
                json={
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "image_b64": image_b64,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "accepted",
                "user_id": "user-1",
                "session_id": "session-1",
            },
        )
        session = app_main.SESSION_STATES["session-1"]
        self.assertEqual(session.user_id, "user-1")
        self.assertEqual(session.latest_image, image_bytes)
        self.assertEqual(session.image_version, 1)
        self.assertTrue(session.search_enqueued)

    def test_similar_search_worker_uses_image_similarity_search(self) -> None:
        search_queue: queue.Queue[str | None] = queue.Queue()
        app_main.SESSION_STATES.clear()
        session = app_main.session_state_for("session-1", "user-1")
        session.tile_client = object()
        with patch.object(app_main, "SEARCH_REQUEST_QUEUE", search_queue):
            session.update_image(b"fake-image")
            search_queue.put(None)

            published_results = [
                {
                    "id": "similar-1",
                    "name": "Similar Jacket",
                    "description": "Denim jacket",
                    "score": 0.91,
                }
            ]

            def discard_coro(coro, _loop):
                coro.close()
                return None

            with (
                patch.object(
                    app_main,
                    "_image_similarity_search",
                    return_value=published_results,
                ) as image_search_mock,
                patch.object(
                    app_main,
                    "_collection_search",
                ) as hybrid_search_mock,
                patch.object(app_main, "MAIN_LOOP", object()),
                patch.object(
                    app_main.asyncio,
                    "run_coroutine_threadsafe",
                    side_effect=discard_coro,
                ) as publish_mock,
            ):
                app_main._search_worker_loop(worker_id=1)

        image_search_mock.assert_called_once_with(b"fake-image")
        hybrid_search_mock.assert_not_called()
        publish_mock.assert_called_once()

    def test_similar_test_endpoint_rejects_invalid_base64(self) -> None:
        with (
            patch.object(app_main, "TEST_ENDPOINTS_ENABLED", True),
            self.make_client() as client,
        ):
            response = client.post(
                "/test/similar",
                json={
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "image_b64": "%%%not-base64%%%",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"detail": "image_b64 must be valid base64"},
        )

    def test_tile_websocket_snapshot_includes_existing_results(self) -> None:
        session = app_main.session_state_for("session-1", "user-1")
        session.similar = [
            {
                "id": "similar-1",
                "name": "Similar Speaker",
                "description": "Compact speaker",
                "score": 0.9,
            }
        ]
        session.recommended = [
            {
                "id": "recommended-1",
                "name": "Recommended Stand",
                "description": "Speaker stand",
                "score": 0.85,
            }
        ]

        with self.make_client() as client:
            with client.websocket_connect("/ws_image_tile/session-1") as websocket:
                payload = websocket.receive_json()

        self.assertEqual(
            payload,
            {
                "kind": "snapshot",
                "sessionId": "session-1",
                "userId": "user-1",
                "similarItems": session.similar,
                "recommendedItems": session.recommended,
            },
        )


if __name__ == "__main__":
    unittest.main()
