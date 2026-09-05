import urllib.request
import os

base_url = "https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/"
files = ["categories.txt", "attributes.txt", "attribute_values.txt"]

os.makedirs("taxonomy_data", exist_ok=True)

for file in files:
    print(f"Downloading {file}...")
    urllib.request.urlretrieve(base_url + file, os.path.join("taxonomy_data", file))
    
print("Done!")
