"""Multimodal search using Vector Search 2.0 with multimodal embeddings."""

from __future__ import annotations

import vertexai
from vertexai.vision_models import MultiModalEmbeddingModel, Image
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import vectorsearch_v1beta

PROJECT_ID = "gcp-samples-ic0"
LOCATION = "us-central1"
COLLECTION_ID = "mercari3m-collection-multimodal"
VECTOR_FIELD = "embedding"

vertexai.init(project=PROJECT_ID, location=LOCATION)
mm_model = MultiModalEmbeddingModel.from_pretrained("multimodalembedding@001")
search_client = vectorsearch_v1beta.DataObjectSearchServiceClient()
data_client = vectorsearch_v1beta.DataObjectServiceClient()
rank_client = discoveryengine.RankServiceClient()
RANKING_CONFIG = f"projects/{PROJECT_ID}/locations/global/rankingConfigs/default_ranking_config"


def _generate_multimodal_embedding(
    text: str | None = None,
    image: bytes | None = None,
) -> list[float]:
    """Generate embedding using multimodalembedding@001 from text or image."""
    if text is not None:
        emb = mm_model.get_embeddings(contextual_text=text)
        return emb.text_embedding
    else:
        emb = mm_model.get_embeddings(image=Image(image_bytes=image))
        return emb.image_embedding


def multimodal_search(
    text: str | None = None,
    image: bytes | None = None,
) -> list[dict]:
    """Search the multimodal collection by text or image.

    Args:
        text: Text query string.
        image: Raw image bytes (JPEG/PNG).

    Returns:
        List of dicts with id, name, and score.
    """
    if text is None and image is None:
        raise ValueError("Either text or image must be provided")

    embedding = _generate_multimodal_embedding(text=text, image=image)

    collection_path = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION_ID}"
    )
    request = vectorsearch_v1beta.SearchDataObjectsRequest(
        parent=collection_path,
        vector_search=vectorsearch_v1beta.VectorSearch(
            search_field=VECTOR_FIELD,
            vector=vectorsearch_v1beta.DenseVector(values=embedding),
            top_k=100,
            output_fields=vectorsearch_v1beta.OutputFields(data_fields=["name", "description"]),
        ),
    )
    response = search_client.search_data_objects(request)

    results = []
    for result in response:
        obj = result.data_object
        item_id = obj.name.split("/")[-1]
        results.append({
            "id": item_id,
            "name": obj.data.get("name", ""),
            "description": obj.data.get("description", ""),
            "score": result.distance,
        })
    return results


def rank_results(query: str, results: list[dict]) -> list[dict]:
    """Re-rank search results using the Vertex AI Ranking API."""
    if not results or not query:
        return results

    records = [
        discoveryengine.RankingRecord(
            id=item["id"], title=item["name"], content=item.get("description", "")
        )
        for item in results
    ]
    request = discoveryengine.RankRequest(
        ranking_config=RANKING_CONFIG,
        query=query,
        records=records,
        top_n=len(records),
    )
    response = rank_client.rank(request=request)

    ranked_by_id = {r.id: r.score for r in response.records}
    for item in results:
        item["score"] = ranked_by_id.get(item["id"], 0.0)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def get_item_details(item_id: str) -> dict | None:
    """Fetch item details from the collection by ID."""
    collection_path = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION_ID}"
    )
    name = f"{collection_path}/dataObjects/{item_id}"
    try:
        obj = data_client.get_data_object(
            vectorsearch_v1beta.GetDataObjectRequest(name=name)
        )
        return {
            "id": item_id,
            "name": obj.data.get("name", ""),
            "description": obj.data.get("description", ""),
            "price": obj.data.get("price", ""),
            "url": obj.data.get("url", ""),
            "img_url": obj.data.get("img_url", ""),
        }
    except Exception:
        return None
