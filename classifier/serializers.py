from rest_framework import serializers
from .models import Category, Product

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'parent']

class ProductSerializer(serializers.ModelSerializer):
    # This automatically nests the Category JSON inside the Product JSON
    predicted_category = CategorySerializer(read_only=True)
    
    class Meta:
        model = Product
        fields = '__all__'