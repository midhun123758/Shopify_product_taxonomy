import os
from celery import shared_task
from .models import Product, Category
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.messages import HumanMessage
import requests
import base64
from pydantic import BaseModel, Field
from typing import List

# 1. NEW: Batch Result Models
class ProductClassificationResult(BaseModel):
    product_id: int = Field(description="The exact database ID of the product")
    category_name: str = Field(description="The exact name of the official Shopify category")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")
    extracted_attributes: dict = Field(description="JSON object of key-value pairs like {'Color': 'Red', 'Material': 'Leather'}")
    alternative_suggestions: list[str] = Field(description="List of 2 backup category names if confidence is low")

class BatchClassificationResult(BaseModel):
    results: List[ProductClassificationResult] = Field(description="List of classification results for all products in this batch")

# 2. NEW: Celery Task processing an array of IDs
@shared_task(rate_limit='4/m', autoretry_for=(Exception,), retry_backoff=True)
def classify_product_batch_task(product_ids):
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
        parser = JsonOutputParser(pydantic_object=BatchClassificationResult)
        
        # Build the mega-prompt for all products
        text_prompt = "You are an expert e-commerce catalog manager.\n"
        text_prompt += "Classify the following products into a single official Shopify category.\n"
        text_prompt += "Extract relevant attributes and suggest backup categories for EACH product.\n\n"
        
        message_content = [{"type": "text", "text": text_prompt}]
        
        for p in products:
            product_text = f"\n--- Product ID {p.id} ---\n"
            product_text += f"Title: {p.title}\n"
            product_text += f"Description: {p.description or 'None'}\n"
            product_text += f"Brand: {p.brand or 'None'}\n"
            product_text += f"Product Type: {p.product_type or 'None'}\n"
            
            message_content.append({"type": "text", "text": product_text})
            
            # Attach the multimodal image!
            if p.image_url and p.image_url.startswith("http"):
                try:
                    response = requests.get(p.image_url, timeout=5)
                    if response.status_code == 200:
                        image_data = base64.b64encode(response.content).decode('utf-8')
                        mime_type = "image/png" if "png" in p.image_url.lower() else "image/jpeg"
                        message_content.append({"type": "text", "text": f"Image for Product ID {p.id}:"})
                        message_content.append({
                            "type": "image_url", 
                            "image_url": {"url": f"data:{mime_type};base64,{image_data}"}
                        })
                except Exception:
                    pass # Image failed, AI falls back to text automatically
                    
        # Append the JSON output requirements
        message_content.append({"type": "text", "text": f"\n\n{parser.get_format_instructions()}"})
        message = HumanMessage(content=message_content)
        
        # Call the AI (1 API request for X products!)
        ai_response = llm.invoke([message])
        batch_result = parser.invoke(ai_response)
        
        # Save results back to DB
        for res in batch_result.get("results", []):
            pid = res.get("product_id")
            product = next((p for p in products if p.id == pid), None)
            if not product:
                continue
                
            predicted_name = res.get("category_name", "")
            confidence = float(res.get("confidence", 0.0))
            
            product.extracted_attributes = res.get("extracted_attributes", {})
            product.alternative_suggestions = res.get("alternative_suggestions", [])
            
            # Extract the leaf node from the AI's predicted string (e.g. 'Home & Garden > Furniture > Sofas' -> 'Sofas')
            leaf_node = predicted_name.split('>')[-1].strip()
            
            # Robustly match the leaf node to the database format (which might not have the 'Home & Garden' root)
            category_match = Category.objects.filter(name__iendswith=f"> {leaf_node}").first()
            if not category_match:
                category_match = Category.objects.filter(name__iexact=leaf_node).first()
                
            if category_match:
                product.predicted_category = category_match
                product.confidence_score = confidence
                product.status = 'COMPLETED' if confidence >= 0.85 else 'MANUAL_REVIEW'
            else:
                product.status = 'MANUAL_REVIEW'
                product.confidence_score = 0.0
                product.alternative_suggestions = [f"Raw AI Output: {predicted_name}"] + product.alternative_suggestions
                
            product.save()
            
        return f"Successfully classified batch of {len(product_ids)} products."
        
    except Exception as e:
        for p in products:
            if p.status == 'PROCESSING':
                p.status = 'FAILED'
                p.save()
        raise e # Explicitly raise so Celery triggers the autoretry_for logic!