from django.urls import path
from .views import (
    ImportExcelAPIView, 
    ProductListAPIView, 
    ReviewListAPIView, 
    ProductUpdateAPIView,
    ProductStatsAPIView,
    PauseProcessingAPIView,
    ResumeProcessingAPIView
)

urlpatterns = [
    path('import/', ImportExcelAPIView.as_view(), name='import-excel'),
    path('products/stats/', ProductStatsAPIView.as_view(), name='product-stats'),
    path('products/', ProductListAPIView.as_view(), name='product-list'),
    path('reviews/', ReviewListAPIView.as_view(), name='review-list'),
    path('products/<int:pk>/', ProductUpdateAPIView.as_view(), name='product-update'),
    path('products/pause/', PauseProcessingAPIView.as_view(), name='product-pause'),
    path('products/resume/', ResumeProcessingAPIView.as_view(), name='product-resume'),
]