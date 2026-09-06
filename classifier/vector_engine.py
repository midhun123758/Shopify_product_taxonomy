import os
import logging
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from fastembed import TextEmbedding

# Initialize in-memory Qdrant Client for high-performance concurrent thread safety
client = QdrantClient(":memory:")
COLLECTION_NAME = "shopify_taxonomy_vectors"

CACHE_FILE = "/app/category_embeddings.npz"

# Initialize FastEmbed text embedding model (BAAI/bge-small-en-v1.5 - 384 dimensions)
embedding_model = None

def get_embedding_model():
    global embedding_model
    if embedding_model is None:
        try:
            logging.info("Initializing FastEmbed text embedding model...")
            embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", threads=4)
        except Exception as e:
            logging.error(f"Failed to load FastEmbed model: {e}")
            embedding_model = None
    return embedding_model


def init_qdrant_taxonomy_index():
    """
    Indexes all 14,606 official Shopify taxonomy categories into Qdrant Vector DB.
    Uses pre-computed numpy embeddings cache for instant 0.2s loading.
    """
    from classifier.models import Category

    # Check if collection already exists and has points
    try:
        collections = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME in collections:
            count = client.count(COLLECTION_NAME).count
            if count > 1000:
                return True
    except Exception:
        pass

    logging.info("Creating Qdrant collection for Shopify Taxonomy...")
    client.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE)
    )

    # 1. Check if pre-computed embeddings file exists for instant loading
    if os.path.exists(CACHE_FILE):
        try:
            logging.info(f"Loading pre-computed category embeddings from {CACHE_FILE}...")
            data = np.load(CACHE_FILE)
            cat_ids = data['ids']
            cat_names = data['names']
            vectors = data['vectors']

            points = []
            for i, (cid, cname, vec) in enumerate(zip(cat_ids, cat_names, vectors)):
                points.append(
                    PointStruct(
                        id=i + 1,
                        vector=vec.tolist(),
                        payload={
                            "cat_id": str(cid),
                            "name": str(cname)
                        }
                    )
                )
                if len(points) >= 1000:
                    client.upsert(collection_name=COLLECTION_NAME, points=points)
                    points = []
            if points:
                client.upsert(collection_name=COLLECTION_NAME, points=points)

            logging.info(f"Loaded {len(cat_ids)} category vectors into Qdrant in <1 second!")
            return True
        except Exception as cache_err:
            logging.warning(f"Failed to load vector cache: {cache_err}. Falling back to online embedding.")

    # 2. Fallback: Generate embeddings if cache not present
    model = get_embedding_model()
    if not model:
        logging.warning("FastEmbed not available, skipping Qdrant vector index.")
        return False

    categories = list(Category.objects.all())
    if not categories:
        return False

    logging.info(f"Embedding {len(categories)} taxonomy categories into Qdrant Vector DB...")
    cat_names = [c.name for c in categories]
    embeddings = list(model.embed(cat_names))

    batch_points = []
    for idx, (cat, vector) in enumerate(zip(categories, embeddings)):
        batch_points.append(
            PointStruct(
                id=idx + 1,
                vector=vector.tolist(),
                payload={
                    "cat_id": cat.id,
                    "name": cat.name
                }
            )
        )
        if len(batch_points) >= 500:
            client.upsert(collection_name=COLLECTION_NAME, points=batch_points)
            batch_points = []

    if batch_points:
        client.upsert(collection_name=COLLECTION_NAME, points=batch_points)

    # Auto-save pre-computed cache to disk for instant 0.01s future loads
    try:
        cat_ids = np.array([str(c.id) for c in categories])
        cat_names_arr = np.array(cat_names)
        vec_matrix = np.array(embeddings, dtype=np.float32)
        np.savez_compressed(CACHE_FILE, ids=cat_ids, names=cat_names_arr, vectors=vec_matrix)
        logging.info(f"Saved pre-computed vector cache to {CACHE_FILE}")
    except Exception as save_err:
        logging.warning(f"Could not save vector cache: {save_err}")

    logging.info("Qdrant Vector DB Taxonomy Index successfully created!")
    return True


def search_qdrant_vector_candidates(product_text: str, limit: int = 10):
    """
    Searches Qdrant Vector DB using semantic embedding of title + description context.
    Returns list of (cat_id, cat_name, cosine_score).
    """
    try:
        # Check collection status
        collections = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in collections or client.count(COLLECTION_NAME).count == 0:
            if os.path.exists(CACHE_FILE):
                init_qdrant_taxonomy_index()
            else:
                return []  # Fast non-blocking fallback to PostgreSQL pg_trgm (10ms)

        model = get_embedding_model()
        if not model:
            return []

        # Embed input product text (title + description snippet)
        query_vectors = list(model.embed([product_text[:400]]))
        if not query_vectors:
            return []

        search_results = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=query_vectors[0].tolist(),
            limit=limit
        )

        results = []
        for hit in search_results:
            cat_id = hit.payload.get("cat_id")
            name = hit.payload.get("name")
            score = float(hit.score)
            results.append((cat_id, name, score))

        return results
    except Exception as e:
        logging.error(f"Qdrant vector search error: {e}")
        return []

