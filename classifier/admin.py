from django.contrib import admin
from .models import Category, Product

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'parent')
    search_fields = ('name', 'id')

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'title', 'status', 'predicted_category', 'confidence_score')
    list_filter = ('status', 'predicted_category')
    search_fields = ('title', 'sku')
