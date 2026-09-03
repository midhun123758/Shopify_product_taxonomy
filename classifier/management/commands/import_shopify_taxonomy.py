import json
import urllib.request
from django.core.management.base import BaseCommand
from classifier.models import Category

class Command(BaseCommand):
    help = 'Fetches and imports the official Shopify Product Taxonomy from GitHub'

    def handle(self, *args, **kwargs):
        url = "https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/categories.json"
        self.stdout.write(f"Downloading taxonomy from {url}...")
        
        try:
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode())
                
            self.stdout.write("Download complete. Parsing categories...")
            
            all_categories = []
            for vertical in data.get('verticals', []):
                for cat in vertical.get('categories', []):
                    all_categories.append(cat)
                    
            # Sort by level so parents are created before children
            all_categories.sort(key=lambda x: x.get('level', 0))
            
            created_count = 0
            for cat in all_categories:
                # Extract the short ID, e.g. "ap-1" from "gid://shopify/TaxonomyCategory/ap-1"
                raw_id = cat.get('id')
                short_id = raw_id.split('/')[-1] if raw_id else None
                
                if not short_id:
                    continue
                    
                raw_parent_id = cat.get('parent_id')
                parent_short_id = raw_parent_id.split('/')[-1] if raw_parent_id else None
                
                parent_obj = None
                if parent_short_id:
                    parent_obj = Category.objects.filter(id=parent_short_id).first()
                
                name_to_store = cat.get('full_name', cat.get('name'))
                
                obj, created = Category.objects.update_or_create(
                    id=short_id,
                    defaults={
                        'name': name_to_store,
                        'parent': parent_obj
                    }
                )
                if created:
                    created_count += 1
                    
            self.stdout.write(self.style.SUCCESS(f"Successfully imported {created_count} categories!"))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error importing taxonomy: {str(e)}"))
