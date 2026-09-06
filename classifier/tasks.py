import os
import json
import re
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


def retrieve_top_category_candidates(product_title: str, product_type: str = '', limit: int = 12):
    """
    Step 1: Pre-fetch top 12 relevant official Shopify categories from PostgreSQL
    using trigonometric similarity (pg_trgm) on title, keywords, and product_type.
    """
    from django.db import connection
    import re

    clean_title = re.sub(r'[^A-Za-z0-9\s]', ' ', product_title)
    words = [w for w in clean_title.split() if len(w) > 3]
    key_phrase = " ".join(words[:4]) if words else clean_title
    query_text = f"{key_phrase} {product_type}".strip()
    
    with connection.cursor() as cursor:
        cursor.execute('''
            SELECT id, name, GREATEST(similarity(name, %s::text), similarity(name, %s::text)) as sim
            FROM classifier_category
            ORDER BY sim DESC
            LIMIT %s;
        ''', [query_text, product_title, limit])
        results = cursor.fetchall()
        
    return results


@shared_task(bind=True, max_retries=5)
def classify_family_task(self, family_id):
    """
    Classify a ProductFamily using an optimized Retrieve-then-Select (RAG) AI Architecture:
    1. Known-Category Result Cache: Auto-classify if an identical title was already processed (0ms).
    2. Pre-fetch top 12 candidate categories from PostgreSQL using pg_trgm similarity.
    3. Resize & compress visual image payload (max 512x512px).
    4. Compact Gemini prompt without reasoning text overhead.
    5. Dynamic rate limit backoff on HTTP 429 / 503.
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

    # Step 1: Pre-fetch top 12 candidates from PostgreSQL database
    candidates = retrieve_top_category_candidates(family.normalized_title, family.product_type or '', limit=12)
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
                img_obj.thumbnail((512, 512)) # Resized for ultra-fast visual inference payload
        except Exception as img_err:
            import logging
            logging.warning(f"Could not load image for family {family.id}: {img_err}")

    # Step 3: Compact prompt without reasoning overhead
    desc_snippet = (family.description or '')[:350]
    prompt = f"""You are an e-commerce product classifier.
Given the product below, select the SINGLE BEST official category ID from the Candidate List provided.

Product: {family.normalized_title}
Type: {family.product_type}
Brand: {family.brand}
Description: {desc_snippet}

Candidate Categories:
{candidates_formatted}

Attributes to Extract if present: material, shape, size, style, color, set_includes

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

        # Clean JSON response if wrapped in markdown codeblocks
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        ai_data = json.loads(text)

        raw_id = str(ai_data.get('selected_category_id', '')).strip()
        selected_id = re.sub(r'^[\[\(\s]*|[\]\)\s]*$', '', raw_id)
        selected_name = str(ai_data.get('category_name', '')).strip()
        confidence = float(ai_data.get('confidence', 0.5))
        attributes = ai_data.get('extracted_attributes', {})

        category_obj = Category.objects.filter(id=selected_id).first() if selected_id else None
        if not category_obj and selected_name:
            category_obj = Category.objects.filter(name__iexact=selected_name).first()

        # Build list of top alternative candidate names directly from DB list
        alt_suggestions = [c[1] for c in candidates if c[0] != selected_id][:4]

        # Validation logic: High confidence (>=0.80) -> COMPLETED, else -> MANUAL_REVIEW
        if category_obj and confidence >= 0.80:
            status = 'COMPLETED'
        else:
            status = 'MANUAL_REVIEW'

        family.predicted_category = category_obj
        family.status = status
        family.confidence_score = confidence
        family.alternative_suggestions = alt_suggestions
        family.extracted_attributes = attributes
        family.save()

        return f"Family {family.id} classified as {status} (Category: {category_obj.name if category_obj else 'None'}, Confidence: {confidence})."

    except Exception as e:
        import logging
        error_str = str(e)
        logging.error(f"Gemini RAG API error for family {family.id}: {e}")

        # Dynamic rate limit retry backoff
        if '429' in error_str:
            retry_delay = 15 + (self.request.retries * 10)
            raise self.retry(exc=e, countdown=retry_delay)

        # Temporary server error — exponential backoff
        if '503' in error_str:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)

        family.status = 'FAILED'
        family.save()
        return f"Failed family {family.id}: {error_str}"


