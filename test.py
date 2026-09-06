import os
import django
import sys
import json

# Setup Django Environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxonomy_project.settings')
django.setup()

from classifier.models import Product, Category
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from typing import List

# Define the exact same Pydantic schema used in tasks.py
class ProductClassificationResult(BaseModel):
    product_id: int = Field(description="The exact database ID of the product")
    category_name: str = Field(description="The exact name of the official Shopify category")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")
    extracted_attributes: dict = Field(description="JSON object of key-value pairs like {'Color': 'Red', 'Material': 'Leather'}")
    alternative_suggestions: list[str] = Field(description="List of 2 backup category names if confidence is low")

class BatchClassificationResult(BaseModel):
    results: List[ProductClassificationResult] = Field(description="List of classification results for all products in this batch")

def run_test(product_id=None):
    print("\n" + "="*50)
    print("🚀 STARTING AI CLASSIFICATION TEST")
    print("="*50)

    # 1. Fetch Product
    if product_id:
        family = ProductFamily.objects.filter(id=product_id).first()
    else:
        family = ProductFamily.objects.filter(status='PENDING').first()

    if not family:
        print("❌ No PENDING families found in the database to test.")
        return

    print(f"\n📦 PRODUCT SELECTED:")
    print(f"   ID: {product.id}")
    print(f"   Title: {product.title}")
    print(f"   Product Type: {product.product_type}")

    # 2. Setup AI
    print("\n🧠 INITIALIZING AI MODEL (gemini-3.5-flash-lite)...")
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        temperature=0.1,
        api_key=os.environ.get("GEMINI_API_KEY")
    )
    parser = JsonOutputParser(pydantic_object=BatchClassificationResult)
    
    # 3. Build Prompt
    text_prompt = "You are an expert e-commerce catalog manager.\n"
    text_prompt += "Classify the following products into a single official Shopify category.\n"
    text_prompt += "Extract relevant attributes and suggest backup categories for EACH product.\n\n"
    
    product_text = f"\n--- Product ID {product.id} ---\n"
    product_text += f"Title: {product.title}\n"
    product_text += f"Description: {product.description or 'None'}\n"
    product_text += f"Brand: {product.brand or 'None'}\n"
    product_text += f"Product Type: {product.product_type or 'None'}\n"
    
    message_content = [{"type": "text", "text": text_prompt}, {"type": "text", "text": product_text}]
    message_content.append({"type": "text", "text": f"\n\n{parser.get_format_instructions()}"})
    
    message = HumanMessage(content=message_content)

    print("\n📡 SENDING REQUEST TO GOOGLE GEMINI API...")
    try:
        ai_response = llm.invoke([message])
        print("✅ Received response from AI!")
    except Exception as e:
        print(f"❌ API Call Failed: {e}")
        return

    # 4. Parse JSON
    print("\n🧩 PARSING RAW AI JSON OUTPUT...")
    try:
        batch_result = parser.invoke(ai_response)
        results = batch_result.get("results", [])
        if not results:
            print("❌ AI returned an empty results list!")
            return
        res = results[0]
        print(json.dumps(res, indent=4))
    except Exception as e:
        print(f"❌ JSON Parsing Failed: {e}")
        print("Raw AI Output:")
        print(ai_response.content)
        return

    # 5. Apply Business Logic
    print("\n⚙️ RUNNING MATCHING ALGORITHM...")
    predicted_name = res.get("category_name", "")
    confidence = float(res.get("confidence", 0.0))
    leaf_node = predicted_name.split('>')[-1].strip()
    
    print(f"   [AI Category]: {predicted_name}")
    print(f"   [Extracted Leaf Node]: '{leaf_node}'")
    print(f"   [AI Confidence]: {confidence}")

    category_match = Category.objects.filter(name__iendswith=f"> {leaf_node}").first()
    if not category_match:
        category_match = Category.objects.filter(name__iexact=leaf_node).first()
        
    if category_match:
        print(f"   ✅ [Database Match Found]: {category_match.name}")
        product.predicted_category = category_match
        product.confidence_score = confidence
        
        if confidence >= 0.86:
            product.status = 'COMPLETED'
            print("   🟢 [Final Status]: COMPLETED (Confidence >= 86%)")
        else:
            product.status = 'MANUAL_REVIEW'
            print("   🟡 [Final Status]: MANUAL_REVIEW (Confidence < 86%)")
    else:
        print(f"   ❌ [No Database Match]: Could not find a category ending with '{leaf_node}'")
        product.status = 'MANUAL_REVIEW'
        product.confidence_score = 0.0
        product.alternative_suggestions = [f"Raw AI Output: {predicted_name}"] + res.get("alternative_suggestions", [])
        print("   🟡 [Final Status]: MANUAL_REVIEW (No Exact DB Match)")

    product.save()
    print("\n💾 Product successfully updated in the database!")
    print("="*50 + "\n")

if __name__ == "__main__":
    test_id = sys.argv[1] if len(sys.argv) > 1 else None
    run_test(test_id)
