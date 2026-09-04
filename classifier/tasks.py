import os
import difflib
import json
from celery import shared_task
from .models import Product, Category
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.messages import HumanMessage
import requests
import base64
from pydantic import BaseModel, Field
from typing import List, Dict, Any

# Load the lightweight taxonomy lookup once at module level (in memory)
TAXONOMY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'simplified_taxonomy.json')
try:
    with open(TAXONOMY_FILE, 'r', encoding='utf-8') as f:
        TAXONOMY_LOOKUP = json.load(f)
except Exception:
    TAXONOMY_LOOKUP = {}

# --- Stage 1 Models (Category Classification) ---
class Stage1Result(BaseModel):
    product_id: int = Field(description="The exact database ID of the product")
    category_name: str = Field(description="The exact name of the official Shopify category")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")
    alternative_suggestions: list[str] = Field(description="List of 2 backup category names if confidence is low")

class Stage1BatchResult(BaseModel):
    results: List[Stage1Result] = Field(description="List of classification results for all products in this batch")

# --- Stage 2 Models (Attribute Extraction) ---
class Stage2Result(BaseModel):
    product_id: int = Field(description="The exact database ID of the product")
    extracted_attributes: dict = Field(description="JSON object of key-value pairs matching ONLY the allowed attributes and values")

class Stage2BatchResult(BaseModel):
    results: List[Stage2Result] = Field(description="List of extracted attributes for all products in this batch")


import redis

@shared_task(rate_limit='4/m', autoretry_for=(Exception,), retry_backoff=True)
def classify_product_batch_task(product_ids):
    # Check if processing is paused
    try:
        r = redis.Redis(host='redis', port=6379, db=0)
        if r.get('PAUSE_AI_PROCESSING') == b'1':
            return f"Aborted batch of {len(product_ids)}: Processing is paused."
    except Exception:
        pass  # If redis fails, continue anyway

    products = list(Product.objects.filter(id__in=product_ids))
    for p in products:
        p.status = 'PROCESSING'
        p.save()

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.5-flash-lite",
            temperature=0.1,
            api_key=os.environ.get("GEMINI_API_KEY", "your-gemini-key-here")
        )
        
        # ==========================================
        # STAGE 1: CATEGORY CLASSIFICATION
        # ==========================================
        parser1 = JsonOutputParser(pydantic_object=Stage1BatchResult)
        
        prompt1 = "You are an expert e-commerce Categorizer.\n"
        prompt1 += "Identify the official Shopify Category for each product.\n"
        prompt1 += "CRITICAL RULE: Shopify removed 'Home & Garden' from its taxonomy. Start furniture categories directly with 'Furniture > ...'\n"
        prompt1 += "Provide confidence scores and alternative backup categories.\n\n"
        
        msg1_content = [{"type": "text", "text": prompt1}]
        
        for p in products:
            product_text = f"\n--- Product ID {p.id} ---\nTitle: {p.title}\nDescription: {p.description or 'None'}\nBrand: {p.brand or 'None'}\nProduct Type: {p.product_type or 'None'}\n"
            msg1_content.append({"type": "text", "text": product_text})
            # Attach image for Stage 1 if available
            if p.image_url and p.image_url.startswith("http"):
                try:
                    response = requests.get(p.image_url, timeout=5)
                    if response.status_code == 200:
                        image_data = base64.b64encode(response.content).decode('utf-8')
                        mime_type = "image/png" if "png" in p.image_url.lower() else "image/jpeg"
                        msg1_content.append({"type": "text", "text": f"Image for Product ID {p.id}:"})
                        msg1_content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}})
                except Exception:
                    pass
                    
        msg1_content.append({"type": "text", "text": f"\n\n{parser1.get_format_instructions()}"})
        
        # Execute Stage 1
        res1 = llm.invoke([HumanMessage(content=msg1_content)])
        stage1_batch = parser1.invoke(res1)
        
        # Prepare for Stage 2
        products_for_stage2 = []
        stage1_dict = {res.get("product_id"): res for res in stage1_batch.get("results", [])}
        
        # Update products with Stage 1 results and fetch allowed attributes
        for p in products:
            s1_res = stage1_dict.get(p.id)
            if not s1_res:
                continue
                
            predicted_name = s1_res.get("category_name", "")
            leaf_node = predicted_name.split('>')[-1].strip()
            
            # 1. Fuzzy match against all known official categories (leaf nodes only)
            leaf_to_full = {path.split('>')[-1].strip(): path for path in TAXONOMY_LOOKUP.keys()}
            leaf_nodes = list(leaf_to_full.keys())
            
            close_matches = difflib.get_close_matches(leaf_node, leaf_nodes, n=1, cutoff=0.65)
            best_leaf_node = close_matches[0] if close_matches else leaf_node
            
            # Lookup category in DB using the best match
            if close_matches:
                full_path = leaf_to_full[best_leaf_node]
                cat_match = Category.objects.filter(name__iexact=full_path).first()
            else:
                cat_match = Category.objects.filter(name__iendswith=f"> {best_leaf_node}").first() or Category.objects.filter(name__iexact=best_leaf_node).first()
            
            if cat_match:
                p.predicted_category = cat_match
                # Default to 0.9 if AI forgets to output a proper confidence
                conf = float(s1_res.get("confidence", 0.9))
                if conf == 0.0: conf = 0.9
                p.confidence_score = conf
                p.alternative_suggestions = s1_res.get("alternative_suggestions", [])
                
                # Fetch allowed attributes from Taxonomy Lookup dictionary
                # Use leaf node or full node to find rules
                allowed_attrs = TAXONOMY_LOOKUP.get(leaf_node, {})
                if not allowed_attrs and cat_match.name in TAXONOMY_LOOKUP:
                    allowed_attrs = TAXONOMY_LOOKUP.get(cat_match.name, {})
                
                if allowed_attrs and p.confidence_score >= 0.85:
                    # Queue for Stage 2 Extraction
                    products_for_stage2.append((p, allowed_attrs))
                else:
                    # Low confidence or no attributes needed -> direct to manual review
                    p.status = 'MANUAL_REVIEW' if p.confidence_score < 0.85 else 'COMPLETED'
            else:
                p.status = 'MANUAL_REVIEW'
                p.confidence_score = 0.0
                p.alternative_suggestions = [f"Raw AI Output: {predicted_name}"] + s1_res.get("alternative_suggestions", [])
                
            p.save()
            
        # ==========================================
        # STAGE 2: ATTRIBUTE EXTRACTION
        # ==========================================
        if products_for_stage2:
            parser2 = JsonOutputParser(pydantic_object=Stage2BatchResult)
            
            prompt2 = "You are an expert Data Extractor.\n"
            prompt2 += "For each product, extract ONLY the requested attributes using ONLY the exact allowed values provided.\n"
            prompt2 += "Do not hallucinate any values. If the product does not match any allowed value, omit the attribute.\n\n"
            
            msg2_content = [{"type": "text", "text": prompt2}]
            
            for p, allowed_attrs in products_for_stage2:
                product_text = f"\n--- Product ID {p.id} ---\nTitle: {p.title}\nDescription: {p.description or 'None'}\nCategory: {p.predicted_category.name}\n"
                product_text += f"\nALLOWED ATTRIBUTES FOR THIS PRODUCT:\n{json.dumps(allowed_attrs, indent=2)}\n"
                msg2_content.append({"type": "text", "text": product_text})
                
            msg2_content.append({"type": "text", "text": f"\n\n{parser2.get_format_instructions()}"})
            
            # Execute Stage 2 (Text only, fast and cheap)
            res2 = llm.invoke([HumanMessage(content=msg2_content)])
            stage2_batch = parser2.invoke(res2)
            
            stage2_dict = {res.get("product_id"): res for res in stage2_batch.get("results", [])}
            
            # Validate and Save Stage 2 results
            for p, allowed_attrs in products_for_stage2:
                s2_res = stage2_dict.get(p.id)
                if not s2_res:
                    continue
                    
                raw_extracted = s2_res.get("extracted_attributes", {})
                validated_extracted = {}
                
                # Strict programmatic validation
                for key, value in raw_extracted.items():
                    if key in allowed_attrs:
                        # Allow exact matches, or list matching if the AI returns a list
                        valid_options = [str(x).lower() for x in allowed_attrs[key]]
                        
                        if isinstance(value, list):
                            validated_vals = [v for v in value if str(v).lower() in valid_options]
                            if validated_vals:
                                validated_extracted[key] = validated_vals
                        elif isinstance(value, str) and value.lower() in valid_options:
                            # Match found (ignoring case) - save the proper cased version from taxonomy
                            matched = next((opt for opt in allowed_attrs[key] if opt.lower() == value.lower()), value)
                            validated_extracted[key] = matched
                            
                p.extracted_attributes = validated_extracted
                p.status = 'COMPLETED'
                p.save()

        return f"Successfully processed batch of {len(product_ids)} products via Two-Stage workflow."
        
    except Exception as e:
        for p in products:
            if p.status == 'PROCESSING':
                p.status = 'FAILED'
                p.save()
        raise e