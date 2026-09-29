import sys

file_path = r'C:\Users\MIDHUN\OneDrive\Desktop\Shopify Product Taxonomy\classifier\tasks.py'

with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find where the shared_task starts
start_idx = 0
for i, line in enumerate(lines):
    if line.startswith('@shared_task(bind=True, max_retries=5)'):
        start_idx = i
        break

rest_of_code = "".join(lines[start_idx:])

head = '''import os
import re
import json
import time
import logging
from celery import shared_task
from django.conf import settings
from .models import ProductFamily, Category
import google.generativeai as genai

def broadcast_update(family):
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "family_updates",
                {
                    "type": "family_update",
                    "id": str(family.id),
                    "status": family.status
                }
            )
    except Exception as e:
        import logging
        logging.error(f"Broadcast update failed for family {family.id}: {e}")

def retrieve_top_category_candidates(product_title: str, product_type: str = '', product_description: str = '', product_brand: str = '', limit: int = 16):
    """
    Clean Generalized 5-Stage RAG Candidate Pipeline:
    1. RETRIEVE: 
       - Qdrant Dense Vector Search (BGE-Small 384d ONNX embeddings over Title + Type + Brand + Description snippet).
       - PostgreSQL pg_trgm Fuzzy Trigram Search (Exact string similarity over Title + Product Type).
    2. FUSE & RANK:
       - Combine vector similarity score and trigram score into a unified mathematical metric:
         Score = (0.65 * Qdrant Vector Score) + (0.35 * PostgreSQL Trigram Score)
       - Sort candidates descending by Combined Hybrid Score.
    """
    from django.db import connection
    import re
    from .vector_engine import search_qdrant_vector_candidates

    clean_title = re.sub(r'[^A-Za-z0-9\s]', ' ', product_title.lower()).strip()
    words = [w for w in clean_title.split() if len(w) > 2]
    
    # Stage 1A: Qdrant Vector Search (Dense Semantic Retrieval over 750 chars)
    full_text_context = f"{product_title} {product_type} {product_brand} {product_description[:750]}".strip()
    qdrant_hits = search_qdrant_vector_candidates(full_text_context, limit=16)

    # Stage 1B: PostgreSQL pg_trgm Search (Lexical Keyword Retrieval)
    combined_query = f"{clean_title} {product_type}".strip()

    with connection.cursor() as cursor:
        cursor.execute(\'\'\'
            SELECT id, name, GREATEST(
                similarity(name, %s::text), 
                similarity(name, %s::text)
            ) as sim
            FROM classifier_category
            WHERE similarity(name, %s::text) > 0.03 OR similarity(name, %s::text) > 0.03 OR name ILIKE %s
            ORDER BY sim DESC
            LIMIT 40;
        \'\'\', [clean_title, combined_query, clean_title, combined_query, f"%{words[0] if words else clean_title}%"])
        raw_candidates = cursor.fetchall()

    # Stage 2: FUSE & RANK (Generalized Mathematical Reranking - No hardcoded domain rules)
    candidate_dict = {}

    for q_id, q_name, q_score in qdrant_hits:
        if q_id:
            candidate_dict[str(q_id)] = {
                "id": str(q_id),
                "name": q_name,
                "qdrant_score": float(q_score),
                "trigram_score": 0.0
            }

    for c_id, c_name, sim in raw_candidates:
        c_str_id = str(c_id)
        if c_str_id in candidate_dict:
            candidate_dict[c_str_id]["trigram_score"] = float(sim)
        else:
            candidate_dict[c_str_id] = {
                "id": c_str_id,
                "name": c_name,
                "qdrant_score": 0.0,
                "trigram_score": float(sim)
            }

    # Calculate combined hybrid score
    scored_candidates = []
    for cid, data in candidate_dict.items():
        combined_score = (0.65 * data["qdrant_score"]) + (0.35 * data["trigram_score"])
        scored_candidates.append((data["id"], data["name"], combined_score))

    # Sort descending by rank score
    scored_candidates.sort(key=lambda x: x[2], reverse=True)

    return scored_candidates[:limit]


'''

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(head + rest_of_code)
