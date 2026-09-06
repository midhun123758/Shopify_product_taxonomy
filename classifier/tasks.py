import os
import re
import json
import time
import logging
from celery import shared_task
from django.conf import settings
from .models import ProductFamily, Category
import google.generativeai as genai

# Load simplified taxonomy
TAXONOMY_FILE = os.path.join(settings.BASE_DIR, 'simplified_taxonomy.json')
with open(TAXONOMY_FILE, 'r') as f:
    TAXONOMY_DATA = json.load(f)


# Build a list of valid category paths for AI matching
VALID_CATEGORIES = []
for category_name, subcategories in TAXONOMY_DATA.items():
    if not subcategories:
        VALID_CATEGORIES.append(category_name)
    else:
        for sub in subcategories:
            VALID_CATEGORIES.append(f"{category_name} > {sub}")


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
        cursor.execute('''
            SELECT id, name, GREATEST(
                similarity(name, %s::text), 
                similarity(name, %s::text)
            ) as sim
            FROM classifier_category
            WHERE similarity(name, %s::text) > 0.03 OR similarity(name, %s::text) > 0.03 OR name ILIKE %s
            ORDER BY sim DESC
            LIMIT 40;
        ''', [clean_title, combined_query, clean_title, combined_query, f"%{words[0] if words else clean_title}%"])
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


@shared_task(bind=True, max_retries=5)
def classify_family_task(self, family_id):
    """
    Classify a ProductFamily using an optimized Retrieve-then-Select (RAG) AI Architecture:
    1. Known-Category Result Cache: Auto-classify if an identical title was already processed (0ms).
    2. Hybrid Retrieval with Combined Reranking & Domain Guardrails.
    3. Resize & compress visual image payload (max 512x512px).
    4. Compact Gemini prompt with pack size / set count directives.
    5. Multi-condition auto-approval verification (Category Exists + Candidate Match + Confidence >= 0.86).
    """
    try:
        family = ProductFamily.objects.get(id=family_id)
    except ProductFamily.DoesNotExist:
        return "Family not found."

    # Step 0: Known-Category Result Cache (0ms Instant Return)
    cached = ProductFamily.objects.filter(
        normalized_title__iexact=family.normalized_title,
        status='COMPLETED',
        predicted_category__isnull=False
    ).exclude(id=family.id).first()

    if cached:
        family.predicted_category = cached.predicted_category
        family.confidence_score = cached.confidence_score
        family.extracted_attributes = cached.extracted_attributes
        family.alternative_suggestions = cached.alternative_suggestions
        family.status = 'COMPLETED'
        family.save()
        return f"Family {family.id} instant auto-classified from result cache."

    # Step 1: True Hybrid Candidates with Weighted Reranking
    candidates = retrieve_top_category_candidates(family.normalized_title, family.product_type or '', family.description or '', family.brand or '', limit=16)
    if not candidates:
        family.status = 'MANUAL_REVIEW'
        family.save()
        return "No candidate categories found."

    candidates_formatted = "\n".join([f"- ID: [{c[0]}] | Name: {c[1]}" for c in candidates])

    # Step 2: Load & Resize product image (512x512 max thumbnail) for fast payload
    first_p = family.products.exclude(image_url='').exclude(image_url__isnull=True).first()
    image_url = first_p.image_url if first_p else None

    img_obj = None
    if image_url:
        try:
            import requests
            from PIL import Image
            from io import BytesIO
            res = requests.get(image_url, timeout=4)
            if res.status_code == 200:
                img_obj = Image.open(BytesIO(res.content))
                img_obj.thumbnail((512, 512))
        except Exception as img_err:
            import logging
            logging.warning(f"Could not load image for family {family.id}: {img_err}")

    # Step 3: Compact prompt with Set Count / Pack Size directives
    desc_snippet = (family.description or '')[:500]
    prompt = f"""You are an e-commerce product classifier.
Given the product below, select the SINGLE BEST official category ID from the Candidate List provided.

Product: {family.normalized_title}
Type: {family.product_type}
Brand: {family.brand}
Description: {desc_snippet}

Candidate Categories:
{candidates_formatted}

Attributes to Extract if present: material, shape, size, style, color, set_includes

CRITICAL CLASSIFICATION DIRECTIVES:
1. Ignore set counts or pack sizes (e.g. 'Set of 2', 'Pack of 4', 'Set of 6'). Classify based on the CORE product item type (e.g. 'Dining Chair'), NOT the set count.
2. Select ONLY a single exact Category ID from the Candidate List provided above.

Respond ONLY in this exact JSON format:
{{
  "selected_category_id": "<exact_id_from_list>",
  "category_name": "<exact_name_from_list>",
  "confidence": 0.95,
  "extracted_attributes": {{"material": "...", "size": "...", "color": "..."}}
}}"""

    try:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-3.6-flash')

        payload = [prompt, img_obj] if img_obj else [prompt]
        response = model.generate_content(payload)
        text = response.text.strip()

        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            text = json_match.group(0)

        ai_data = json.loads(text)

        raw_id = str(ai_data.get('selected_category_id', '')).strip()
        selected_id = re.sub(r'^[\[\(\s]*|[\]\)\s]*$', '', raw_id)
        selected_name = str(ai_data.get('category_name', '')).strip()
        confidence = float(ai_data.get('confidence', 0.5))
        attributes = ai_data.get('extracted_attributes', {})

        # Strict Category Database Lookup (Strict ID or Exact Name match - NO loose contains fallbacks)
        category_obj = Category.objects.filter(id=selected_id).first() if selected_id else None
        if not category_obj and selected_name:
            category_obj = Category.objects.filter(name__iexact=selected_name).first()

        candidate_ids = [str(c[0]) for c in candidates]
        candidate_names = [c[1] for c in candidates]
        alt_suggestions = [c[1] for c in candidates if str(c[0]) != str(selected_id)][:4]

        # Multi-Condition Validation Logic for Auto-Approval:
        # 1. Category exists in DB via strict lookup
        # 2. Selected category is present in top retrieved candidates
        # 3. Confidence score >= 0.86
        is_candidate_match = category_obj and (str(category_obj.id) in candidate_ids or category_obj.name in candidate_names)

        if category_obj and is_candidate_match and confidence >= 0.86:
            status = 'COMPLETED'
        else:
            status = 'MANUAL_REVIEW'

        family.predicted_category = category_obj
        family.status = status
        family.confidence_score = confidence
        family.alternative_suggestions = alt_suggestions
        family.extracted_attributes = attributes
        family.save()

        # Slight pacing pause to stay safely under Gemini 15 RPM Free Tier limit
        import time
        time.sleep(2)

        return f"Family {family.id} classified as {status} (Category: {category_obj.name if category_obj else 'None'}, Confidence: {confidence})."

    except Exception as e:
        import logging
        error_str = str(e)
        logging.error(f"Gemini RAG API error for family {family.id}: {e}")

        # Dynamic rate limit retry backoff using Google's exact requested delay
        if '429' in error_str or 'ResourceExhausted' in error_str:
            retry_delay = 35
            delay_match = re.search(r'retry\s+in\s+([\d\.]+)', error_str, re.IGNORECASE)
            if not delay_match:
                delay_match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', error_str)
            if delay_match:
                retry_delay = int(float(delay_match.group(1))) + 2
            raise self.retry(exc=e, countdown=retry_delay)

        # Temporary server error — exponential backoff
        if '503' in error_str:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)

        family.status = 'FAILED'
        family.save()
        return f"Failed family {family.id}: {error_str}"


@shared_task
def process_all_pending_families_task(limit=50):
    """
    Batched background task that processes PENDING or FAILED product families.
    Enforces pacing between tasks to stay comfortably under Gemini Rate Limits (15 RPM).
    """
    from django.utils import timezone
    from datetime import timedelta
    # Reset stuck items older than 5 minutes
    stuck_cutoff = timezone.now() - timedelta(minutes=5)
    ProductFamily.objects.filter(status='PROCESSING', updated_at__lt=stuck_cutoff).update(status='PENDING')

    pending_families = ProductFamily.objects.filter(status__in=['PENDING', 'FAILED']).order_by('id')[:limit]
    count = 0
    for family in pending_families:
        classify_family_task.delay(family.id)
        count += 1
        time.sleep(1.5)
    return f"Queued {count} product families for AI classification."



