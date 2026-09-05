import os
import json
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


def guarded_fuzzy_match(raw_ai_output: str, brand: str) -> tuple[Category | None, str, list[str]]:   
    import re
    from django.db import connection
    
    clean_output = raw_ai_output.replace('Raw AI Output: ', '').strip()
    match = re.search(r'([A-Za-z0-9_& /-]+ > [A-Za-z0-9_& /-]+(?: > [A-Za-z0-9_& /-]+)?)', clean_output)
    
    predicted_path = match.group(1) if match else clean_output
    predicted_path = predicted_path.strip()

    if not predicted_path:
        return None, "FAILED", []

    leaf_node = predicted_path.split('>')[-1].strip()
    parent_path_expected = ">".join([p.strip() for p in predicted_path.split('>')[:-1]])

    # 1. ILIKE search with leaf node or full output
    with connection.cursor() as cursor:
        cursor.execute('''
            SELECT id, name, similarity(name, %s::text) as sim
            FROM classifier_category
            WHERE name ILIKE %s::text OR name ILIKE %s::text
            ORDER BY sim DESC
            LIMIT 5;
        ''', [clean_output, f'%{leaf_node}%', f'%{clean_output}%'])
        results = cursor.fetchall()

    # 2. Fallback trigram similarity using max of full path vs leaf node
    if not results:
        with connection.cursor() as cursor:
            cursor.execute('''
                SELECT id, name, GREATEST(similarity(name, %s::text), similarity(name, %s::text)) as sim
                FROM classifier_category
                ORDER BY sim DESC
                LIMIT 5;
            ''', [clean_output, leaf_node])
            results = cursor.fetchall()

    if not results:
        return None, "MANUAL_REVIEW", []

    best_match_id, best_match_name, best_match_sim = results[0]

    alt_suggestions = [row[1] for row in results[1:]] if len(results) > 1 else []

    if parent_path_expected:
        best_match_parents = ">".join([p.strip() for p in best_match_name.split('>')[:-1]])
        if best_match_parents.lower() != parent_path_expected.lower():
            if best_match_sim < 0.6:
                return None, "MANUAL_REVIEW", alt_suggestions
            else:
                return None, "MANUAL_REVIEW", [best_match_name] + alt_suggestions

    if best_match_sim > 0.4:
        category = Category.objects.get(id=best_match_id)
        if brand and brand.lower() in category.name.lower():
            return None, "MANUAL_REVIEW", [best_match_name] + alt_suggestions
        return category, "COMPLETED", alt_suggestions
    else:
        return None, "MANUAL_REVIEW", [best_match_name] + alt_suggestions

@shared_task(bind=True, max_retries=5)
def classify_family_task(self, family_id):
    """
    Classify a single ProductFamily using Gemini.
    Uses a short prompt to stay within free-tier token limits,
    then fuzzy-matches the response to our real category database.
    """
    try:
        family = ProductFamily.objects.get(id=family_id)
    except ProductFamily.DoesNotExist:
        return "Family not found."

    # Load product image if URL exists for Multimodal AI analysis
    first_p = family.products.exclude(image_url='').exclude(image_url__isnull=True).first()
    image_url = first_p.image_url if first_p else None

    img_obj = None
    if image_url:
        try:
            import requests
            from PIL import Image
            from io import BytesIO
            res = requests.get(image_url, timeout=5)
            if res.status_code == 200:
                img_obj = Image.open(BytesIO(res.content))
        except Exception as img_err:
            import logging
            logging.warning(f"Could not load image for family {family.id}: {img_err}")

    # Include title, type, brand, and description text for full context
    desc_snippet = (family.description or '')[:600]
    prompt = f"""You are an e-commerce product classifier.
Given the product below, analyze both its visual product image (if provided) and text details to output the best Shopify taxonomy category path using this format:
  Category: <Top Level> > <Sub Category> > <Leaf Category>
Then extract key attributes as compact JSON.

Product: {family.normalized_title}
Type: {family.product_type}
Brand: {family.brand}
Description: {desc_snippet}

Respond ONLY in this format (no extra text):
Category: ...
Attributes: {{"material": "...", "style": "...", "size": "..."}}"""

    try:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-3.6-flash')

        payload = [prompt, img_obj] if img_obj else [prompt]
        response = model.generate_content(payload)
        text = response.text

        import re
        cat_match = re.search(r'Category:\s*(.+)', text, re.IGNORECASE)
        attr_match = re.search(r'Attributes:\s*(\{.*?\})', text, re.DOTALL | re.IGNORECASE)

        raw_category = cat_match.group(1).strip() if cat_match else text.strip()
        attributes = {}
        if attr_match:
            try:
                attributes = json.loads(attr_match.group(1))
            except:
                pass

        category, status, alts = guarded_fuzzy_match(raw_category, family.brand)

        family.predicted_category = category
        family.status = status
        family.alternative_suggestions = alts
        family.extracted_attributes = attributes
        family.confidence_score = 0.95 if status == "COMPLETED" else 0.4
        family.save()
        return f"Family {family.id} classified as {status}."

    except Exception as e:
        import logging
        error_str = str(e)
        logging.error(f"Gemini API error for family {family.id}: {e}")

        # Rate limit — retry after the suggested delay (default 60s)
        if '429' in error_str:
            retry_delay = 60
            import re as re2
            delay_match = re2.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', error_str)
            if delay_match:
                retry_delay = int(delay_match.group(1)) + 5
            raise self.retry(exc=e, countdown=retry_delay)

        # Temporary server error — exponential backoff
        if '503' in error_str:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)

        family.status = 'FAILED'
        family.save()
        return f"Failed family {family.id}: {error_str}"

