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
    title = str(title).lower().strip()
    # Normalize extra spaces
    title = re.sub(r'\s+', ' ', title).strip()
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
            known_colors = ['black', 'white', 'blue', 'red', 'green', 'yellow', 'brown', 'grey', 'gray', 'pink', 'purple', 'gold', 'silver', 'bronze', 'brass', 'beige', 'cream', 'navy', 'teal', 'tan']
            products_to_create = []
            for row in df.to_dict('records'):
                raw_sku = str(row.get('sku', '')).strip()
                final_sku = raw_sku if raw_sku else None
                family_obj = family_map.get(str(row['normalized_title']))

                title_str = str(row.get('title', ''))
                color_val = str(row.get('color', '')).strip() if row.get('color') else None
                if not color_val and '-' in title_str:
                    possible_color = title_str.split('-')[-1].strip().lower()
                    if any(kc in possible_color for kc in known_colors):
                        color_val = title_str.split('-')[-1].strip()

                products_to_create.append(
                    Product(
                        sku=final_sku,
                        family=family_obj,
                        title=title_str,
                        description=str(row.get('description', '')),
                        product_type=str(row.get('product_type', '')),
                        brand=str(row.get('brand', '')),
                        image_url=str(row.get('image_url', '')),
                        color=color_val
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
    GET /api/families/?search=...&status=...
    Returns the grouped families for the UI to display.
    """
    serializer_class = ProductFamilySerializer

    def get_queryset(self):
        queryset = ProductFamily.objects.all().order_by('-id')
        search_param = self.request.query_params.get('search', None)
        if search_param:
            queryset = queryset.filter(
                Q(normalized_title__icontains=search_param) |
                Q(brand__icontains=search_param) |
                Q(product_type__icontains=search_param) |
                Q(predicted_category__name__icontains=search_param)
            )
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
        family = get_object_or_404(ProductFamily, pk=pk)
        
        category = None
        if 'category_id' in request.data:
            category = Category.objects.filter(id=request.data['category_id']).first()
        elif 'category_name' in request.data:
            name = str(request.data['category_name']).strip()
            # Exact full name match first
            category = Category.objects.filter(name__iexact=name).first()
            if not category:
                # Substring/icontains fallback
                category = Category.objects.filter(name__icontains=name).first()
                
            if not category:
                return Response({"error": f"Could not find a category matching '{name}'"}, status=status.HTTP_400_BAD_REQUEST)
                
        if category:
            family.predicted_category = category
            family.status = 'COMPLETED'
            family.save()
            return Response({"message": "Family successfully manually categorized.", "new_category": category.name}, status=status.HTTP_200_OK)
            
        return Response({"error": "Please provide a valid category_id or category_name"}, status=status.HTTP_400_BAD_REQUEST)


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
            from .tasks import process_all_pending_families_task
            process_all_pending_families_task.delay(limit=100)
            return Response({"message": "AI Processing RESUMED. Started background batch classification for pending families."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ClearDataAPIView(APIView):
    """
    POST /api/products/clear/
    Wipes all Product and ProductFamily records from DB and flushes Celery task queues in Redis.
    """
    def post(self, request, *args, **kwargs):
        try:
            p_deleted, _ = Product.objects.all().delete()
            f_deleted, _ = ProductFamily.objects.all().delete()
            
            redis_msg = "Redis queue cleared."
            try:
                try:
                    r = redis.Redis(host='redis', port=6379, db=0)
                    r.flushdb()
                except Exception:
                    r = redis.Redis(host='localhost', port=6379, db=0)
                    r.flushdb()
            except Exception as re:
                redis_msg = f"Redis queue warning: {re}"

            return Response({
                "message": "Product data and Redis queue successfully wiped.",
                "deleted_products": p_deleted,
                "deleted_families": f_deleted,
                "redis_status": redis_msg
            }, status=status.HTTP_200_OK)
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


class CategorySearchAPIView(APIView):
    """
    GET /api/categories/search/?q=dining
    Fuzzy / trigram & substring search across Shopify Taxonomy categories.
    """
    def get(self, request, *args, **kwargs):
        query = request.query_params.get('q', '').strip()
        if not query:
            # Return root categories (no '>' in name or parent is null)
            roots = Category.objects.filter(~Q(name__contains='>')).order_by('name')[:40]
            results = []
            for c in roots:
                results.append({
                    'id': c.id,
                    'name': c.name,
                    'leaf_name': c.name,
                    'depth': 1,
                    'breadcrumb': [c.name],
                    'similarity': 1.0
                })
            return Response(results, status=status.HTTP_200_OK)

        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('''
                SELECT id, name, similarity(name, %s::text) as sim
                FROM classifier_category
                WHERE name ILIKE %s OR similarity(name, %s::text) > 0.15
                ORDER BY sim DESC, name ASC
                LIMIT 50;
            ''', [query, f"%{query}%", query])
            rows = cursor.fetchall()

        results = []
        for r in rows:
            cat_id, cat_name, sim = r[0], r[1], r[2]
            parts = [p.strip() for p in cat_name.split('>')]
            results.append({
                'id': cat_id,
                'name': cat_name,
                'leaf_name': parts[-1] if parts else cat_name,
                'depth': len(parts),
                'breadcrumb': parts,
                'similarity': float(sim or 0)
            })

        return Response(results, status=status.HTTP_200_OK)


class CategoryWayfindAPIView(APIView):
    """
    GET /api/categories/wayfind/?category_id=... or GET /api/categories/<id>/wayfind/
    Returns full hierarchy details for category wayfinding:
    - Breadcrumb navigation list
    - Immediate subcategories / children
    - Count and sample of product families categorized under this node
    """
    def get(self, request, pk=None, *args, **kwargs):
        cat_id = pk or request.query_params.get('category_id')
        if not cat_id:
            return Response({"error": "Category ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        category = get_object_or_404(Category, pk=cat_id)
        parts = [p.strip() for p in category.name.split('>')]

        # Build breadcrumbs in single bulk DB query
        path_strs = [" > ".join(parts[:i+1]) for i in range(len(parts))]
        matching_cats = {c.name.lower(): c.id for c in Category.objects.filter(name__in=path_strs)}

        breadcrumbs = []
        for i, part in enumerate(parts):
            full_path = path_strs[i]
            breadcrumbs.append({
                'level': i + 1,
                'name': part,
                'full_path': full_path,
                'id': matching_cats.get(full_path.lower())
            })

        # Subcategories (Children) in single bulk DB query
        prefix = f"{category.name} > "
        child_qs = Category.objects.filter(name__startswith=prefix)
        seen_child_leaves = {}
        for child in child_qs:
            sub = child.name[len(prefix):]
            child_leaf = sub.split('>')[0].strip()
            if child_leaf not in seen_child_leaves:
                full_child_name = f"{prefix}{child_leaf}"
                seen_child_leaves[child_leaf] = full_child_name

        child_full_names = list(seen_child_leaves.values())
        matching_child_cats = {c.name.lower(): c.id for c in Category.objects.filter(name__in=child_full_names)}

        children = []
        for child_leaf, full_child_name in seen_child_leaves.items():
            cat_id_found = matching_child_cats.get(full_child_name.lower(), category.id)
            children.append({
                'id': cat_id_found,
                'name': child_leaf,
                'full_name': full_child_name
            })

        # Product Families under this category (exact or prefix subcategory match)
        assigned_families = ProductFamily.objects.filter(
            Q(predicted_category=category) | Q(predicted_category__name__startswith=prefix)
        ).order_by('-id')
        
        assigned_count = assigned_families.count()
        sample_families = ProductFamilySerializer(assigned_families[:12], many=True).data

        return Response({
            'id': category.id,
            'name': category.name,
            'depth': len(parts),
            'breadcrumbs': breadcrumbs,
            'children': children,
            'assigned_families_count': assigned_count,
            'assigned_families': sample_families
        }, status=status.HTTP_200_OK)


class BrandListAPIView(APIView):
    """
    GET /api/brands/
    Returns a grouped list of all distinct product brands in the database,
    along with product counts, family counts, and sample products.
    """
    def get(self, request, *args, **kwargs):
        from django.db.models import Q
        
        families = ProductFamily.objects.all().select_related('predicted_category')
        
        brand_map = {}
        for f in families:
            b_name = (f.brand or '').strip()
            if not b_name:
                b_name = 'Unbranded'
            
            if b_name not in brand_map:
                brand_map[b_name] = {
                    'brand_name': b_name,
                    'total_families': 0,
                    'completed_families': 0,
                    'total_products': 0,
                    'categories': set(),
                    'sample_products': []
                }
            
            brand_map[b_name]['total_families'] += 1
            if f.status == 'COMPLETED':
                brand_map[b_name]['completed_families'] += 1
            if f.predicted_category:
                brand_map[b_name]['categories'].add(f.predicted_category.name.split('>')[-1].strip())

        all_products = Product.objects.all().select_related('family')
        for p in all_products:
            b_name = (p.brand or (p.family.brand if p.family else '') or '').strip()
            if not b_name:
                b_name = 'Unbranded'
            
            if b_name in brand_map:
                brand_map[b_name]['total_products'] += 1
                if len(brand_map[b_name]['sample_products']) < 6:
                    brand_map[b_name]['sample_products'].append({
                        'id': p.id,
                        'title': p.title,
                        'sku': p.sku,
                        'image_url': p.image_url,
                        'color': p.color,
                        'product_type': p.product_type
                    })

        result = []
        for b_name, data in brand_map.items():
            result.append({
                'brand_name': data['brand_name'],
                'total_families': data['total_families'],
                'completed_families': data['completed_families'],
                'total_products': data['total_products'],
                'categories': list(data['categories'])[:5],
                'sample_products': data['sample_products']
            })

        result.sort(key=lambda x: x['total_products'], reverse=True)
        return Response(result, status=status.HTTP_200_OK)


class BrandDetailAPIView(APIView):
    """
    GET /api/brands/detail/?brand=...
    Returns full product family and product list for a specific brand.
    """
    def get(self, request, *args, **kwargs):
        from django.db.models import Q
        brand_name = request.query_params.get('brand', '').strip()
        if not brand_name:
            return Response({"error": "Brand query parameter is required"}, status=status.HTTP_400_BAD_REQUEST)

        if brand_name.lower() == 'unbranded':
            families = ProductFamily.objects.filter(Q(brand__isnull=True) | Q(brand='') | Q(brand__iexact='unbranded'))
            products = Product.objects.filter(Q(brand__isnull=True) | Q(brand='') | Q(brand__iexact='unbranded'))
        else:
            families = ProductFamily.objects.filter(brand__iexact=brand_name)
            products = Product.objects.filter(brand__iexact=brand_name)

        families_data = ProductFamilySerializer(families, many=True).data
        products_data = ProductSerializer(products, many=True).data

        return Response({
            'brand_name': brand_name,
            'total_families': families.count(),
            'total_products': products.count(),
            'families': families_data,
            'products': products_data
        }, status=status.HTTP_200_OK)


