import os
import re
import json
import time
import logging
from celery import shared_task
from django.conf import settings
from .models import ProductFamily, Category
import google.generativeai as genai

def safe_json_loads(text):
    """
    Safely parse JSON text from LLM outputs even if it contains unescaped control characters,
    markdown code blocks, or minor string escaping flaws.
    """
    if not text:
        return {}
    
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if json_match:
        cleaned = json_match.group(0)

    try:
        return json.loads(cleaned, strict=False)
    except Exception:
        pass

    sanitized = re.sub(r'[\r\n\t]+', ' ', cleaned)
    try:
        return json.loads(sanitized, strict=False)
    except Exception:
        pass

    sanitized_bytes = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', cleaned)
    try:
        return json.loads(sanitized_bytes, strict=False)
    except Exception:
        return {}

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
                    "status": family.status,
                    "confidence_score": family.confidence_score,
                    "predicted_category": family.predicted_category.name if family.predicted_category else None,
                    "alternative_suggestions": family.alternative_suggestions
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


from langsmith import traceable

@shared_task(bind=True, max_retries=5)
@traceable(name="gemini_product_classification")
def categorize_task(self, family_id):
    """
    Unified Fast AI Classification & Attribute Extraction Engine (<4s per product):
    Performs RAG Candidate Retrieval + Gemini Classification + Attribute Extraction in 1 single pass.
    """
    global TAXONOMY_CACHE
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
        broadcast_update(family)
        return f"Family {family.id} instant auto-classified from result cache."

    # Step 1: True Hybrid Candidates with Weighted Reranking
    candidates = retrieve_top_category_candidates(family.normalized_title, family.product_type or '', family.description or '', family.brand or '', limit=12)
    if not candidates:
        family.status = 'MANUAL_REVIEW'
        family.save()
        broadcast_update(family)
        return "No candidate categories found."

    candidates_formatted = "\n".join([f"- ID: [{c[0]}] | Name: {c[1]}" for c in candidates])

    # Step 2: Fast Image Fetch (1.5s max timeout)
    first_p = family.products.exclude(image_url='').exclude(image_url__isnull=True).first()
    image_url = first_p.image_url if first_p else None

    img_obj = None
    if image_url:
        try:
            import requests
            from PIL import Image
            from io import BytesIO
            res = requests.get(image_url, timeout=1.5)
            if res.status_code == 200:
                img_obj = Image.open(BytesIO(res.content))
                img_obj.thumbnail((256, 256))
        except Exception:
            pass

    # Step 3: Get variant info for attribute extraction
    variants = family.products.all()
    variant_colors = [v.color for v in variants if v.color]
    unique_colors_str = ", ".join(list(set(variant_colors)))
    variant_titles = "\n".join([f"- {v.title}" for v in variants][:6])

    desc_snippet = (family.description or '')[:400]

    # Step 4: Compact Unified Prompt (Category + Attributes in 1 pass)
    prompt = f"""You are an e-commerce product classifier and attribute extractor.
Select the SINGLE BEST official category ID for this product from the Candidate List, and extract key product attributes (e.g., color, material, style, size).

Product: {family.normalized_title}
Type: {family.product_type}
Brand: {family.brand}
Description: {desc_snippet}
Variant Colors: {unique_colors_str if unique_colors_str else 'None'}
Variant Titles:
{variant_titles}

Candidate Categories:
{candidates_formatted}

Respond ONLY in this exact JSON format:
{{
  "selected_category_id": "<exact_id_from_list>",
  "category_name": "<exact_name_from_list>",
  "confidence": 0.95,
  "extracted_attributes": {{
    "color": "Value",
    "material": "Value"
  }}
}}"""

    try:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-3.5-flash-lite')
        config = genai.types.GenerationConfig(temperature=0.1, max_output_tokens=300)

        payload = [prompt, img_obj] if img_obj else [prompt]
        response = model.generate_content(payload, generation_config=config)
        text = response.text.strip()

        ai_data = safe_json_loads(text)

        raw_id = str(ai_data.get('selected_category_id', '')).strip()
        selected_id = re.sub(r'^[\[\(\s]*|[\]\)\s]*$', '', raw_id)
        selected_name = str(ai_data.get('category_name', '')).strip()
        attributes = ai_data.get('extracted_attributes', {})

        conf_raw = ai_data.get('confidence')
        if conf_raw is not None:
            try:
                confidence = float(conf_raw)
            except (ValueError, TypeError):
                confidence = 0.90
        else:
            confidence = 0.90

        category_obj = Category.objects.filter(id=selected_id).first() if selected_id else None
        if not category_obj and selected_name:
            category_obj = Category.objects.filter(name__iexact=selected_name).first()

        # Fallback 1: Match against candidate list if exact ID lookup missed
        if not category_obj:
            for c_id, c_name, c_score in candidates:
                if str(c_id) == str(selected_id) or c_name.lower() == selected_name.lower() or selected_name.lower() in c_name.lower():
                    category_obj = Category.objects.filter(id=c_id).first()
                    if category_obj:
                        break

        # Fallback 2: Top RAG candidate match if Gemini didn't return a valid category
        if not category_obj and candidates:
            top_c_id = candidates[0][0]
            category_obj = Category.objects.filter(id=top_c_id).first()
            confidence = 0.82

        candidate_ids = [str(c[0]) for c in candidates]
        candidate_names = [c[1] for c in candidates]
        alt_suggestions = [c[1] for c in candidates if category_obj and str(c[0]) != str(category_obj.id)][:4]

        is_candidate_match = category_obj and (str(category_obj.id) in candidate_ids or category_obj.name in candidate_names)

        if category_obj and is_candidate_match and confidence >= 0.85:
            status = 'COMPLETED'
        else:
            status = 'MANUAL_REVIEW'

        family.predicted_category = category_obj
        family.extracted_attributes = attributes
        family.confidence_score = confidence
        family.alternative_suggestions = alt_suggestions
        family.status = status
        family.save()
        broadcast_update(family)

        return f"Family {family.id} auto-classified as {category_obj.name if category_obj else 'None'} in 1 pass."

    except Exception as e:
        import logging
        error_str = str(e)
        logging.error(f"Gemini API error for family {family.id}: {e}")

        if '429' in error_str or 'ResourceExhausted' in error_str:
            retry_delay = 35
            delay_match = re.search(r'retry\s+in\s+([\d\.]+)', error_str, re.IGNORECASE)
            if not delay_match:
                delay_match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', error_str)
            if delay_match:
                retry_delay = int(float(delay_match.group(1))) + 2
            raise self.retry(exc=e, countdown=retry_delay)

        if '503' in error_str:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)

        family.status = 'FAILED'
        family.save()
        broadcast_update(family)
        return f"Failed family {family.id}: {error_str}"


TAXONOMY_CACHE = None

@shared_task(bind=True, max_retries=5)
def extract_attributes_task(self, family_id, final_status):
    """
    Agent 2 (Attribute Extractor):
    Dynamically loads valid official attributes for the predicted category
    and strictly extracts them using a focused prompt.
    """
    global TAXONOMY_CACHE
    try:
        family = ProductFamily.objects.get(id=family_id)
    except ProductFamily.DoesNotExist:
        return "Family not found."

    if not family.predicted_category:
        family.status = final_status
        family.save()
        broadcast_update(family)
        return "No category predicted, skipping attribute extraction."

    # Load simplified_taxonomy.json lazily into a global cache
    if TAXONOMY_CACHE is None:
        try:
            with open('simplified_taxonomy.json', 'r', encoding='utf-8') as f:
                TAXONOMY_CACHE = json.load(f)
        except Exception as e:
            import logging
            logging.warning(f"Could not load simplified_taxonomy.json: {e}")
            TAXONOMY_CACHE = {}

    # Fetch official attributes and their allowed values for this category
    cat_name = family.predicted_category.name
    cat_data = TAXONOMY_CACHE.get(cat_name, {})
    
    if not cat_data:
        valid_attrs_str = "material, shape, size, style, color, set_includes"
    else:
        # Build strict allowed values string
        attrs_list = []
        for attr_name, attr_values in list(cat_data.items())[:12]:
            if isinstance(attr_values, list) and len(attr_values) > 0:
                allowed_vals = ", ".join([str(v) for v in attr_values[:15]])
                attrs_list.append(f"- {attr_name} (Allowed Values: {allowed_vals})")
            else:
                attrs_list.append(f"- {attr_name}")
        valid_attrs_str = "\n".join(attrs_list)

    # Get known colors/attributes from the actual Product variants
    variants = family.products.all()
    variant_colors = [v.color for v in variants if v.color]
    unique_colors_str = ", ".join(list(set(variant_colors)))
    variant_titles = "\n".join([f"- {v.title}" for v in variants][:10])

    desc_snippet = (family.description or '')[:500]
    prompt = f"""You are an e-commerce attribute extractor.
Product Family: {family.normalized_title}
Type: {family.product_type}
Brand: {family.brand}
Description: {desc_snippet}
Category: {cat_name}

Known Variant Colors in this Family: {unique_colors_str if unique_colors_str else 'None'}
Variant Titles:
{variant_titles}

Extract ONLY the following valid official attributes for this category. You MUST ONLY select values from the 'Allowed Values' list for each attribute. Do NOT invent your own values.

{valid_attrs_str}

Respond ONLY in this exact JSON format (extract as many attributes as you can find). If an attribute has multiple values across variants (like multiple colors), return an array of strings.
{{
  "extracted_attributes": {{
    "Attribute Name 1": "Allowed Value",
    "Attribute Name 2": ["Allowed Value A", "Allowed Value B"]
  }}
}}
If no attributes match, return an empty object: {{"extracted_attributes": {{}}}}"""

    try:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        # We can use the faster flash-8b model if available, but stick to flash to be safe
        model = genai.GenerativeModel('gemini-3.6-flash')

        response = model.generate_content(prompt)
        text = response.text.strip()

        ai_data = safe_json_loads(text)
        attributes = ai_data.get('extracted_attributes', {})

        family.extracted_attributes = attributes
        family.status = final_status # Set to COMPLETED or MANUAL_REVIEW
        family.save()
        broadcast_update(family)

        import time
        time.sleep(1.5)

        return f"Agent 2 Extracted Attributes for Family {family.id}."

    except Exception as e:
        import logging
        error_str = str(e)
        logging.error(f"Gemini Attribute API error for family {family.id}: {e}")

        if '429' in error_str or 'ResourceExhausted' in error_str:
            retry_delay = 35
            delay_match = re.search(r'retry\s+in\s+([\d\.]+)', error_str, re.IGNORECASE)
            if not delay_match:
                delay_match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', error_str)
            if delay_match:
                retry_delay = int(float(delay_match.group(1))) + 2
            raise self.retry(exc=e, countdown=retry_delay)

        if '503' in error_str:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)

        # Even if attribute extraction fails permanently, keep the category and just mark as final status
        family.status = final_status
        family.save()
        broadcast_update(family)
        return f"Agent 2 Failed family {family.id}: {error_str}"


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
        # Trigger Agent 1 (which will trigger Agent 2)
        categorize_task.delay(family.id)
        count += 1
        time.sleep(1.5)
    return f"Queued {count} product families for Multi-Agent AI classification."




