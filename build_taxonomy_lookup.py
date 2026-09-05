import json
import urllib.request
import os

TAXONOMY_URL = "https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/taxonomy.json"
OUTPUT_FILE = "simplified_taxonomy.json"

def build_lookup():
    print(f"Downloading {TAXONOMY_URL}...")
    req = urllib.request.Request(TAXONOMY_URL)
    with urllib.request.urlopen(req) as response:
        print("Parsing JSON...")
        data = json.loads(response.read().decode('utf-8'))
        
    print("Building lightweight lookup...")
    
    # 1. Build an attribute lookup: attribute_id -> { "name": "Color", "values": ["Red", "Blue"] }
    attr_lookup = {}
    for attr in data.get('attributes', []):
        attr_id = attr['id']
        attr_name = attr['name']
        values = [val['name'] for val in attr.get('values', [])]
        attr_lookup[attr_id] = {
            "name": attr_name,
            "values": values
        }
        
    # 2. Build the category lookup: category_name -> { "attributes": { "Color": ["Red", "Blue"] } }
    # Also support mapping leaf node names if they are unique enough
    category_lookup = {}
    
    for vertical in data.get('verticals', []):
        for cat in vertical.get('categories', []):
            # The API gives full_name or name. We'll store both as keys to make lookup robust.
            full_name = cat.get('full_name', cat['name'])
            leaf_name = cat['name']
            
            # Resolve the attributes for this category
            resolved_attrs = {}
            for attr_ref in cat.get('attributes', []):
                attr_id = attr_ref['id']
                if attr_id in attr_lookup:
                    attr_data = attr_lookup[attr_id]
                    resolved_attrs[attr_data['name']] = attr_data['values']
                    
            # Store in lookup by leaf name (for simple AI output matching) and full name (for exact matching)
            # If leaf name collides, it will overwrite, but full_name is safe.
            category_lookup[leaf_name] = resolved_attrs
            category_lookup[full_name] = resolved_attrs

    print(f"Saving {len(category_lookup)} category mappings to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(category_lookup, f, indent=2)
        
    print("Done! Taxonomy lookup successfully built.")

if __name__ == "__main__":
    build_lookup()
