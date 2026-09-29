import os
import django
import sys

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxonomy_project.settings')
django.setup()

from classifier.models import ProductFamily

def create_bulk_products():
    print("Creating 10 dummy products for bulk testing...")
    
    families = []
    for i in range(1, 11):
        title = f"Test Bulk Furniture Item {i}"
        family = ProductFamily(
            normalized_title=title.lower(),
            description="A nice piece of test furniture",
            product_type="Furniture",
            brand="TestBrand",
            status="PENDING"
        )
        families.append(family)
        
    ProductFamily.objects.bulk_create(families, ignore_conflicts=True)
    
    print("Successfully created 10 dummy products!")
    print("Go to your Live Dashboard on the frontend, and click 'Resume Processing' to watch the bulk processing in real-time!")

if __name__ == '__main__':
    create_bulk_products()
