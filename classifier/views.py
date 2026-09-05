import pandas as pd
import re
from rest_framework.views import APIView
from rest_framework import generics
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from rest_framework.parsers import MultiPartParser
from drf_spectacular.utils import extend_schema
from rest_framework import serializers

from .models import Product, ProductFamily, Category
from .serializers import ProductSerializer, CategorySerializer
from django.db.models import Q
import redis

# We'll create serializers for Family inline or assume we create them later
class ProductFamilySerializer(serializers.ModelSerializer):
    # Pull a representative image from the first variant in the family
    image_url = serializers.SerializerMethodField()
    # Human-readable category name instead of just the FK id (e.g. "fr-24")
    predicted_category_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductFamily
        fields = '__all__'

    def get_image_url(self, obj):
        first_product = obj.products.exclude(image_url='').exclude(image_url__isnull=True).first()
        return first_product.image_url if first_product else None

    def get_predicted_category_name(self, obj):
        if obj.predicted_category:
            return obj.predicted_category.name
        return None

class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()

def clean_title(title):
    # Extremely basic grouping logic for the assignment
    title = str(title).lower().strip()
    # Remove things like " - black", " - set of 4"
    title = re.sub(r'\s*-\s*.*$', '', title)
    return title

class ImportExcelAPIView(APIView):
    """
    POST /api/products/import/
    Uploads an Excel file, uses Pandas for extreme speed, 
    bulk inserts 10,000+ items, groups them into families, 
    but DOES NOT trigger AI automatically.
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
            df = pd.read_excel(file, engine='openpyxl')
            df.columns = df.columns.str.lower().str.strip().str.replace(' ', '_')
            
            shopify_mapping = {
                'product_number': 'sku',
                'product_name': 'title',
                'product_description': 'description',
                'product_category': 'product_type',
                'image_1': 'image_url'
            }
            df.rename(columns=shopify_mapping, inplace=True)
            df = df.fillna('')
            
            # --- PANDAS CLUSTERING / GROUPING LOGIC ---
            # Create a normalized family title
            df['normalized_title'] = df['title'].apply(clean_title)
            
            # 1. Create Product Families first
            unique_families_df = df.drop_duplicates(subset=['normalized_title'])
            families_to_create = []
            for row in unique_families_df.to_dict('records'):
                families_to_create.append(
                    ProductFamily(
                        normalized_title=str(row['normalized_title']),
                        description=str(row.get('description', '')),
                        product_type=str(row.get('product_type', '')),
                        brand=str(row.get('brand', '')),
                        status='PENDING'
                    )
                )
            # Bulk create ignoring conflicts if they already exist
            ProductFamily.objects.bulk_create(families_to_create, ignore_conflicts=True)
            
            # Re-fetch families to get their IDs
            family_objs = ProductFamily.objects.filter(normalized_title__in=unique_families_df['normalized_title'].tolist())
            family_map = {f.normalized_title: f for f in family_objs}
            
            # 2. Create individual Products linked to families
            products_to_create = []
            for row in df.to_dict('records'):
                raw_sku = str(row.get('sku', '')).strip()
                final_sku = raw_sku if raw_sku else None
                family_obj = family_map.get(str(row['normalized_title']))

                products_to_create.append(
                    Product(
                        sku=final_sku,
                        family=family_obj,
                        title=str(row.get('title', '')),
                        description=str(row.get('description', '')),
                        product_type=str(row.get('product_type', '')),
                        brand=str(row.get('brand', '')),
                        image_url=str(row.get('image_url', '')),
                        # Extract basic color from title if it exists after dash
                        color=str(row.get('title', '')).split('-')[-1].strip() if '-' in str(row.get('title', '')) else None
                    )
                )

            Product.objects.bulk_create(products_to_create, ignore_conflicts=True)

            return Response({
                "message": f"Successfully imported {len(products_to_create)} variants grouped into {len(family_objs)} families.",
                "total_families": len(family_objs),
                "total_variants": len(products_to_create)
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            import logging
            logging.error(f"Excel Upload Failed: {str(e)}")
            return Response({"error": "An internal error occurred during processing. Please check the file format or try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProductFamilyListAPIView(generics.ListAPIView):
    """
    GET /api/families/
    Returns the grouped families for the UI to display.
    """
    serializer_class = ProductFamilySerializer

    def get_queryset(self):
        queryset = ProductFamily.objects.all().order_by('-id')
        status_param = self.request.query_params.get('status', None)
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset

class AnalyzeFamilyAPIView(APIView):
    """
    POST /api/families/<id>/analyze/
    Triggers the AI classification specifically for this one product family.
    """
    def post(self, request, pk, *args, **kwargs):
        family = get_object_or_404(ProductFamily, pk=pk)
        if family.status == 'COMPLETED':
            return Response({"message": "Already classified."}, status=status.HTTP_400_BAD_REQUEST)
            
        family.status = 'PROCESSING'
        family.save()
        
        # Trigger celery just for this family ID
        from .tasks import classify_family_task
        classify_family_task.delay(family.id)
        
        return Response({"message": f"Family '{family.normalized_title}' queued for AI classification."}, status=status.HTTP_200_OK)


class ProductListAPIView(generics.ListAPIView):
    serializer_class = ProductSerializer

    def get_queryset(self):
        return Product.objects.all().order_by('-id')

class MainCategoryListAPIView(APIView):
    def get(self, request, *args, **kwargs):
        main_categories = Category.objects.filter(~Q(name__contains='>')).order_by('name').values_list('name', flat=True)
        return Response(list(main_categories), status=status.HTTP_200_OK)

class ReviewListAPIView(generics.ListAPIView):
    serializer_class = ProductFamilySerializer
    def get_queryset(self):
        return ProductFamily.objects.filter(status='MANUAL_REVIEW').order_by('-id')

class ProductUpdateSerializer(serializers.Serializer):
    category_id = serializers.CharField(required=False, help_text="The ID of the new category")
    category_name = serializers.CharField(required=False, help_text="The name of the new category to look up")

class ProductUpdateAPIView(APIView):
    serializer_class = ProductUpdateSerializer

    def patch(self, request, pk, *args, **kwargs):
        # We'll adapt this for ProductFamily instead of Product since families hold the status
        family = get_object_or_404(ProductFamily, pk=pk)
        
        category = None
        if 'category_id' in request.data:
            category = get_object_or_404(Category, id=request.data['category_id'])
        elif 'category_name' in request.data:
            name = request.data['category_name'].replace('Raw AI Output: ', '').strip()
            leaf_node = name.split('>')[-1].strip()
            category = Category.objects.filter(name__iendswith=f"> {leaf_node}").first()
            if not category:
                category = Category.objects.filter(name__iexact=leaf_node).first()
                
            if not category:
                return Response({"error": f"Could not find a category matching '{name}'"}, status=status.HTTP_400_BAD_REQUEST)
                
        if category:
            family.predicted_category = category
            family.status = 'COMPLETED'
            family.save()
            return Response({"message": "Family successfully manually categorized.", "new_category": category.name}, status=status.HTTP_200_OK)
            
        return Response({"error": "Please provide a category_id or category_name"}, status=status.HTTP_400_BAD_REQUEST)

class ProductStatsAPIView(APIView):
    def get(self, request, *args, **kwargs):
        stats = {
            "pending": ProductFamily.objects.filter(status='PENDING').count(),
            "processing": ProductFamily.objects.filter(status='PROCESSING').count(),
            "completed": ProductFamily.objects.filter(status='COMPLETED').count(),
            "review": ProductFamily.objects.filter(status='MANUAL_REVIEW').count(),
            "total_families": ProductFamily.objects.count(),
            "total_variants": Product.objects.count()
        }
        return Response(stats, status=status.HTTP_200_OK)

class PauseProcessingAPIView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            r = redis.Redis(host='redis', port=6379, db=0)
            r.set('PAUSE_AI_PROCESSING', '1')
            return Response({"message": "AI Processing has been PAUSED. No new batches will start."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ResumeProcessingAPIView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            r = redis.Redis(host='redis', port=6379, db=0)
            r.set('PAUSE_AI_PROCESSING', '0')
            return Response({"message": "AI Processing RESUMED. (Bulk queueing not implemented in Pandas On-Demand mode yet)."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class FamilyDetailAPIView(APIView):
    """
    GET /api/families/<id>/
    Returns a single family + all its product variants.
    """
    def get(self, request, pk, *args, **kwargs):
        family = get_object_or_404(ProductFamily, pk=pk)
        family_data = ProductFamilySerializer(family).data
        products = Product.objects.filter(family=family).order_by('id')
        products_data = ProductSerializer(products, many=True).data
        return Response({
            **family_data,
            'products': products_data
        }, status=status.HTTP_200_OK)


class ProductDetailAPIView(APIView):
    """
    GET /api/products/<id>/detail/
    Returns a single product with its family's AI classification data.
    """
    def get(self, request, pk, *args, **kwargs):
        product = get_object_or_404(Product, pk=pk)
        product_data = ProductSerializer(product).data
        family = product.family
        family_data = ProductFamilySerializer(family).data if family else {}
        return Response({
            **product_data,
            'family': family_data
        }, status=status.HTTP_200_OK)