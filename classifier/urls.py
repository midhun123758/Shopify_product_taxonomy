from django.urls import path
from .views import (
    ImportExcelAPIView,
    ProductListAPIView,
    ProductFamilyListAPIView,
    AnalyzeFamilyAPIView,
    MainCategoryListAPIView,
    ReviewListAPIView,
    ProductUpdateAPIView,
    ProductStatsAPIView,
    PauseProcessingAPIView,
    ResumeProcessingAPIView,
    FamilyDetailAPIView,
    ProductDetailAPIView
)

urlpatterns = [
    path('products/import/', ImportExcelAPIView.as_view(), name='import_excel'),
    path('products/', ProductListAPIView.as_view(), name='product_list'),
    path('families/', ProductFamilyListAPIView.as_view(), name='family_list'),
    path('families/<int:pk>/', FamilyDetailAPIView.as_view(), name='family_detail'),
    path('families/<int:pk>/analyze/', AnalyzeFamilyAPIView.as_view(), name='family_analyze'),
    path('products/<int:pk>/detail/', ProductDetailAPIView.as_view(), name='product_detail'),
    path('products/review/', ReviewListAPIView.as_view(), name='review_list'),
    path('products/<int:pk>/', ProductUpdateAPIView.as_view(), name='product_update'),
    path('products/stats/', ProductStatsAPIView.as_view(), name='product_stats'),
    path('categories/main/', MainCategoryListAPIView.as_view(), name='main_categories'),
    path('products/pause/', PauseProcessingAPIView.as_view(), name='pause_processing'),
    path('products/resume/', ResumeProcessingAPIView.as_view(), name='resume_processing'),
]