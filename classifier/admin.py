from django.contrib import admin
from .models import Category, Product, ProductFamily

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'parent')
    search_fields = ('name',)

@admin.register(ProductFamily)
class ProductFamilyAdmin(admin.ModelAdmin):
    list_display = ('id', 'normalized_title', 'status', 'predicted_category', 'confidence_score')
    list_filter = ('status', 'predicted_category')
    search_fields = ('normalized_title', 'description', 'brand', 'product_type')

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'sku', 'title', 'family', 'brand')
    list_filter = ('family__status',)
    search_fields = ('sku', 'title', 'brand')
