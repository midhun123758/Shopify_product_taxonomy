from django.urls import path
from .views import (
    ImportExcelAPIView, 
    ProductListAPIView, 
    ReviewListAPIView, 
    ProductUpdateAPIView
)

urlpatterns = [
    path('import/', ImportExcelAPIView.as_view(), name='import-excel'),
    path('products/', ProductListAPIView.as_view(), name='product-list'),
    path('reviews/', ReviewListAPIView.as_view(), name='review-list'),
    path('products/<int:pk>/', ProductUpdateAPIView.as_view(), name='product-update'),
]