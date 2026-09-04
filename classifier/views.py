import pandas as pd
from rest_framework.views import APIView
from rest_framework import generics
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser
from drf_spectacular.utils import extend_schema
from rest_framework import serializers

from .models import Product, Category
from .serializers import ProductSerializer, CategorySerializer
from .tasks import classify_product_batch_task

class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()

class ImportExcelAPIView(APIView):
    """
    POST /api/products/import/
    Uploads an Excel file, uses Pandas for extreme speed, 
    bulk inserts 10,000+ items, and triggers the AI Celery batches.
    """
    parser_classes = (MultiPartParser,)
    serializer_class = UploadSerializer

    @extend_schema(
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'file': {
                        'type': 'string',
                        'format': 'binary'
                    }
                }
            }
        }
    )
    def post(self, request, *args, **kwargs):
        file = request.FILES.get('file')
        if not file:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 1. Use Pandas to read the Excel file (Maximum Speed)
            df = pd.read_excel(file, engine='openpyxl')
            
            # CLEAN HEADERS: Automatically convert 'Product Type' to 'product_type', 'SKU' to 'sku', etc.
            print("=========================================")
            print("ORIGINAL EXCEL HEADERS:", df.columns.tolist())
            print("=========================================")
            # CLEAN HEADERS: Convert to lowercase, replace spaces with underscores, and strip trailing whitespace!
            df.columns = df.columns.str.lower().str.strip().str.replace(' ', '_')
            
            # CUSTOM MAPPING: Match the exact headers from the user's Excel file
            shopify_mapping = {
                'product_number': 'sku',
                'product_name': 'title',
                'product_description': 'description',
                'product_category': 'product_type',
                'image_1': 'image_url'
            }
            df.rename(columns=shopify_mapping, inplace=True)
            
            # FILL NA: Pandas puts 'NaN' for empty cells, we must replace them with empty strings
            df = df.fillna('')
            
            # 2. Build the list of products for Bulk Creation
            products_to_create = []
            for index, row in df.iterrows():
                # Extract SKU: If it's an empty string, we MUST set it to None to avoid unique constraint violations
                raw_sku = str(row.get('sku', '')).strip()
                final_sku = raw_sku if raw_sku else None

                # We use .get() so if a column is missing, it doesn't crash! (Requirement #5)
                products_to_create.append(
                    Product(
                        sku=final_sku,
                        title=str(row.get('title', '')),
                        description=str(row.get('description', '')),
                        product_type=str(row.get('product_type', '')),
                        brand=str(row.get('brand', '')),
                        image_url=str(row.get('image_url', '')),
                        status='PENDING' # They are pending until Celery runs
                    )
                )

            # 3. Bulk Insert (Solves Database Bottleneck)
            Product.objects.bulk_create(products_to_create, ignore_conflicts=True)
            
            # 4. Trigger Celery AI Workers in Batches (Solves AI Rate Limits & Time)
            # We fetch all the PENDING product IDs from the database
            pending_products = Product.objects.filter(status='PENDING')
            product_ids = [p.id for p in pending_products]
            
            # Batch them into groups of 18 for Celery
            for i in range(0, len(product_ids), 18):
                batch = product_ids[i:i+18]
                classify_product_batch_task.delay(batch) # This sends the array of IDs to Redis!

            return Response({
                "message": f"Successfully imported {len(products_to_create)} products.",
                "total_queued_for_ai": len(product_ids)
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProductListAPIView(generics.ListAPIView):
    """
    GET /api/products/?status=COMPLETED
    Returns products (optionally filtered by status) with pagination.
    """
    serializer_class = ProductSerializer

    def get_queryset(self):
        queryset = Product.objects.all().order_by('-id')
        status_param = self.request.query_params.get('status', None)
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class ReviewListAPIView(generics.ListAPIView):
    """
    GET /api/products/review/
    Returns ONLY products where the AI had low confidence (Requirement #9).
    Paginated automatically.
    """
    serializer_class = ProductSerializer
    queryset = Product.objects.filter(status='MANUAL_REVIEW').order_by('-id')


class ProductUpdateSerializer(serializers.Serializer):
    category_id = serializers.CharField(required=False, help_text="The ID of the new category")
    category_name = serializers.CharField(required=False, help_text="The name of the new category to look up")

class ProductUpdateAPIView(APIView):
    """
    PATCH /api/products/<id>/
    Allows a human to manually approve or change a category (Requirement #12).
    """
    serializer_class = ProductUpdateSerializer

    def patch(self, request, pk, *args, **kwargs):
        product = get_object_or_404(Product, pk=pk)
        
        category = None
        if 'category_id' in request.data:
            category = get_object_or_404(Category, id=request.data['category_id'])
        elif 'category_name' in request.data:
            # Robust matching: Try to match leaf node if full path not matched perfectly
            name = request.data['category_name'].replace('Raw AI Output: ', '').strip()
            leaf_node = name.split('>')[-1].strip()
            
            category = Category.objects.filter(name__iendswith=f"> {leaf_node}").first()
            if not category:
                category = Category.objects.filter(name__iexact=leaf_node).first()
                
            if not category:
                return Response({"error": f"Could not find a category matching '{name}'"}, status=status.HTTP_400_BAD_REQUEST)
                
        if category:
            product.predicted_category = category
            product.status = 'COMPLETED' # Human approved it!
            product.save()
            return Response({"message": "Product successfully manually categorized.", "new_category": category.name}, status=status.HTTP_200_OK)
            
        return Response({"error": "Please provide a category_id or category_name"}, status=status.HTTP_400_BAD_REQUEST)

class ProductStatsAPIView(APIView):
    """
    GET /api/products/stats/
    Instantly returns the counts of products by status using SQL aggregation.
    """
    def get(self, request, *args, **kwargs):
        stats = {
            "pending": Product.objects.filter(status='PENDING').count(),
            "processing": Product.objects.filter(status='PROCESSING').count(),
            "completed": Product.objects.filter(status='COMPLETED').count(),
            "review": Product.objects.filter(status='MANUAL_REVIEW').count(),
            "total": Product.objects.count()
        }
        return Response(stats, status=status.HTTP_200_OK)

import redis

class PauseProcessingAPIView(APIView):
    """
    POST /api/products/pause/
    Sets a global flag in Redis to stop Celery workers from starting new batches.
    """
    def post(self, request, *args, **kwargs):
        try:
            r = redis.Redis(host='redis', port=6379, db=0)
            r.set('PAUSE_AI_PROCESSING', '1')
            return Response({"message": "AI Processing has been PAUSED. No new batches will start."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ResumeProcessingAPIView(APIView):
    """
    POST /api/products/resume/
    Removes the global pause flag and re-queues any remaining PENDING products.
    """
    def post(self, request, *args, **kwargs):
        try:
            r = redis.Redis(host='redis', port=6379, db=0)
            r.set('PAUSE_AI_PROCESSING', '0')
            
            # Re-queue all pending products
            pending_products = Product.objects.filter(status='PENDING')
            product_ids = [p.id for p in pending_products]
            
            for i in range(0, len(product_ids), 18):
                batch = product_ids[i:i+18]
                classify_product_batch_task.delay(batch)
                
            return Response({"message": f"AI Processing RESUMED. {len(product_ids)} products re-queued."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)