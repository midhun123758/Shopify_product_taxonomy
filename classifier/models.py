from django.db import models

class Category(models.Model):
    """
    Stores the official Shopify Taxonomy categories.
    Example Shopify ID: 'sg-4-17-2-17'
    """
    id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')

    def __str__(self):
        return f"{self.name} ({self.id})"

class ProductFamily(models.Model):
    normalized_title = models.CharField(max_length=500, unique=True)
    description = models.TextField(null=True, blank=True)
    product_type = models.CharField(max_length=255, null=True, blank=True)
    brand = models.CharField(max_length=255, null=True, blank=True)
    
    status = models.CharField(
        max_length=50, 
        choices=[
            ('PENDING', 'Pending AI Classification'),
            ('PROCESSING', 'AI is Processing'),
            ('COMPLETED', 'Classification Complete'),
            ('MANUAL_REVIEW', 'Needs Manual Review'),
            ('FAILED', 'Processing Failed')
        ],
        default='PENDING'
    )
    
    predicted_category = models.ForeignKey('Category', on_delete=models.SET_NULL, null=True, blank=True)
    confidence_score = models.FloatField(null=True, blank=True)
    alternative_suggestions = models.JSONField(null=True, blank=True)
    extracted_attributes = models.JSONField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.normalized_title

class Product(models.Model):
    sku = models.CharField(max_length=100, unique=True, null=True, blank=True)
    family = models.ForeignKey(ProductFamily, on_delete=models.CASCADE, related_name='products', null=True, blank=True)
    title = models.CharField(max_length=500)
    description = models.TextField(null=True, blank=True)
    
    product_type = models.CharField(max_length=255, null=True, blank=True)
    brand = models.CharField(max_length=255, null=True, blank=True)
    image_url = models.URLField(max_length=1000, null=True, blank=True)
    
    # Optional variant attributes extracted during Pandas grouping
    color = models.CharField(max_length=100, null=True, blank=True)
    size = models.CharField(max_length=100, null=True, blank=True)
    
    # Individual product's own extracted attributes (which inherit from family but can be overridden)
    extracted_attributes = models.JSONField(null=True, blank=True) 
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.title