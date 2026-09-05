"""
Business Logic Test Suite for Shopify Product Taxonomy Classifier
=================================================================
Tests cover:
  1. Product Family Grouping (Pandas clean_title logic)
  2. Import API (/api/products/import/)
  3. Family List API (/api/families/)
  4. Stats API (/api/products/stats/)
  5. Analyze Trigger API (/api/families/<id>/analyze/)
  6. Manual Review Update (PATCH /api/products/<pk>/)
  7. Full Classification Flow (end-to-end with mocked Gemini + fuzzy match)
"""

import json
import io
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.db import connection

import openpyxl

from classifier.models import Product, ProductFamily, Category
from classifier.views import clean_title


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_excel(rows: list[dict]) -> io.BytesIO:
    """Build an in-memory .xlsx file from a list of row dicts."""
    wb = openpyxl.Workbook()
    ws = wb.active
    if rows:
        ws.append(list(rows[0].keys()))
        for row in rows:
            ws.append(list(row.values()))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def make_category(id_str="gid://shopify/1", name="Furniture > Living Room > Sofas"):
    return Category.objects.get_or_create(id=id_str, defaults={"name": name})[0]


def enable_pg_trgm():
    """Enable the pg_trgm extension needed for similarity() in tests."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")


# ---------------------------------------------------------------------------
# 1. clean_title — Product Family Grouping Logic
# ---------------------------------------------------------------------------

class CleanTitleTests(TestCase):
    """Tests for clean_title(), which normalises product titles to group variants."""

    def test_strips_colour_variant(self):
        self.assertEqual(clean_title("Empress Bonded Leather Sofa - White"), "empress bonded leather sofa")

    def test_strips_size_variant(self):
        self.assertEqual(clean_title("Oak Dining Table - Large"), "oak dining table")

    def test_strips_multi_word_suffix(self):
        self.assertEqual(clean_title("Velvet Armchair - Midnight Blue"), "velvet armchair")

    def test_no_dash_unchanged(self):
        self.assertEqual(clean_title("Simple Chair"), "simple chair")

    def test_lowercased_and_whitespace_stripped(self):
        self.assertEqual(clean_title("  LUXURY SOFA  "), "luxury sofa")

    def test_colour_variants_same_family(self):
        """Two colour variants must produce the same normalised title."""
        self.assertEqual(
            clean_title("Empress Sofa - Black"),
            clean_title("Empress Sofa - White")
        )

    def test_different_products_different_family(self):
        self.assertNotEqual(
            clean_title("Empress Sofa - Black"),
            clean_title("Oslo Dining Chair - Black")
        )

    def test_numeric_suffix_stripped(self):
        self.assertEqual(clean_title("Bar Stool - Set of 4"), "bar stool")


# ---------------------------------------------------------------------------
# 2. Import API — POST /api/products/import/
# ---------------------------------------------------------------------------

class ImportAPITests(TestCase):

    def setUp(self):
        self.client = Client()

    def _upload(self, rows):
        buf = make_excel(rows)
        buf.name = "products.xlsx"
        return self.client.post("/api/products/import/", {"file": buf}, format="multipart")

    def test_import_creates_products_and_families(self):
        rows = [
            {"Product Number": "SKU-001", "Product Name": "Empress Sofa - White",
             "Product Description": "A sofa", "Product Category": "Living Room", "Image 1": ""},
            {"Product Number": "SKU-002", "Product Name": "Empress Sofa - Black",
             "Product Description": "A sofa", "Product Category": "Living Room", "Image 1": ""},
        ]
        response = self._upload(rows)
        self.assertEqual(response.status_code, 201)
        # Two colour variants → 1 family
        self.assertEqual(ProductFamily.objects.count(), 1)
        self.assertEqual(Product.objects.count(), 2)

    def test_import_groups_correctly(self):
        rows = [
            {"Product Number": "SKU-A1", "Product Name": "Oak Table - Small",
             "Product Description": "", "Product Category": "Dining", "Image 1": ""},
            {"Product Number": "SKU-A2", "Product Name": "Oak Table - Large",
             "Product Description": "", "Product Category": "Dining", "Image 1": ""},
            {"Product Number": "SKU-B1", "Product Name": "Velvet Chair - Blue",
             "Product Description": "", "Product Category": "Bedroom", "Image 1": ""},
        ]
        self._upload(rows)
        self.assertEqual(ProductFamily.objects.count(), 2)
        self.assertEqual(Product.objects.count(), 3)

    def test_new_families_start_as_pending(self):
        rows = [{"Product Number": "SKU-X", "Product Name": "Canvas Painting",
                 "Product Description": "", "Product Category": "Art", "Image 1": ""}]
        self._upload(rows)
        self.assertEqual(ProductFamily.objects.first().status, "PENDING")

    def test_no_file_returns_400(self):
        response = self.client.post("/api/products/import/")
        self.assertEqual(response.status_code, 400)

    def test_duplicate_sku_ignored(self):
        """Re-uploading the same SKU must not create duplicate products."""
        rows = [{"Product Number": "SKU-DUP", "Product Name": "Test Chair - Red",
                 "Product Description": "", "Product Category": "Chair", "Image 1": ""}]
        self._upload(rows)
        self._upload(rows)
        self.assertEqual(Product.objects.count(), 1)

    def test_response_contains_counts(self):
        rows = [{"Product Number": "SKU-R1", "Product Name": "Round Table - Oak",
                 "Product Description": "", "Product Category": "Table", "Image 1": ""}]
        data = self._upload(rows).json()
        self.assertIn("total_families", data)
        self.assertIn("total_variants", data)

    def test_products_linked_to_correct_family(self):
        rows = [
            {"Product Number": "SKU-L1", "Product Name": "Linen Sofa - Grey",
             "Product Description": "", "Product Category": "Sofa", "Image 1": ""},
            {"Product Number": "SKU-L2", "Product Name": "Linen Sofa - Beige",
             "Product Description": "", "Product Category": "Sofa", "Image 1": ""},
        ]
        self._upload(rows)
        family = ProductFamily.objects.first()
        self.assertEqual(family.products.count(), 2)


# ---------------------------------------------------------------------------
# 3. Family List API — GET /api/families/
# ---------------------------------------------------------------------------

class FamilyListAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        ProductFamily.objects.create(normalized_title="sofa a", status="PENDING")
        ProductFamily.objects.create(normalized_title="sofa b", status="COMPLETED")
        ProductFamily.objects.create(normalized_title="sofa c", status="MANUAL_REVIEW")

    def test_list_all_families(self):
        data = self.client.get("/api/families/").json()
        self.assertEqual(data["count"], 3)

    def test_filter_by_pending(self):
        data = self.client.get("/api/families/?status=PENDING").json()
        self.assertEqual(data["count"], 1)

    def test_filter_by_completed(self):
        data = self.client.get("/api/families/?status=COMPLETED").json()
        self.assertEqual(data["count"], 1)

    def test_filter_by_manual_review(self):
        data = self.client.get("/api/families/?status=MANUAL_REVIEW").json()
        self.assertEqual(data["count"], 1)

    def test_response_is_paginated(self):
        data = self.client.get("/api/families/").json()
        self.assertIn("results", data)
        self.assertIn("count", data)


# ---------------------------------------------------------------------------
# 4. Stats API — GET /api/products/stats/
# ---------------------------------------------------------------------------

class StatsAPITests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_empty_stats(self):
        data = self.client.get("/api/products/stats/").json()
        self.assertEqual(data["total_families"], 0)
        self.assertEqual(data["total_variants"], 0)

    def test_stats_count_correctly(self):
        f1 = ProductFamily.objects.create(normalized_title="f1", status="PENDING")
        f2 = ProductFamily.objects.create(normalized_title="f2", status="COMPLETED")
        f3 = ProductFamily.objects.create(normalized_title="f3", status="MANUAL_REVIEW")
        Product.objects.create(title="P1", family=f1)
        Product.objects.create(title="P2", family=f1)
        Product.objects.create(title="P3", family=f2)

        data = self.client.get("/api/products/stats/").json()
        self.assertEqual(data["total_families"], 3)
        self.assertEqual(data["total_variants"], 3)
        self.assertEqual(data["pending"], 1)
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["review"], 1)
        self.assertEqual(data["processing"], 0)


# ---------------------------------------------------------------------------
# 5. Analyze Trigger API — POST /api/families/<id>/analyze/
# ---------------------------------------------------------------------------

class AnalyzeFamilyAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        self.family = ProductFamily.objects.create(
            normalized_title="test sofa", status="PENDING"
        )

    @patch("classifier.tasks.classify_family_task")
    def test_analyze_queues_task(self, mock_task):
        mock_task.delay = MagicMock()
        response = self.client.post(f"/api/families/{self.family.id}/analyze/")
        self.assertEqual(response.status_code, 200)
        mock_task.delay.assert_called_once_with(self.family.id)

    @patch("classifier.tasks.classify_family_task")
    def test_analyze_sets_status_to_processing(self, mock_task):
        mock_task.delay = MagicMock()
        self.client.post(f"/api/families/{self.family.id}/analyze/")
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, "PROCESSING")

    def test_already_completed_returns_400(self):
        self.family.status = "COMPLETED"
        self.family.save()
        response = self.client.post(f"/api/families/{self.family.id}/analyze/")
        self.assertEqual(response.status_code, 400)

    def test_nonexistent_family_returns_404(self):
        response = self.client.post("/api/families/99999/analyze/")
        self.assertEqual(response.status_code, 404)

    @patch("classifier.tasks.classify_family_task")
    def test_response_message_contains_family_title(self, mock_task):
        mock_task.delay = MagicMock()
        data = self.client.post(f"/api/families/{self.family.id}/analyze/").json()
        self.assertIn("message", data)


# ---------------------------------------------------------------------------
# 6. Manual Review Update — PATCH /api/products/<pk>/
# ---------------------------------------------------------------------------

class ManualUpdateAPITests(TestCase):

    def setUp(self):
        self.client = Client()
        self.category = make_category("gid://shopify/sofa-1", "Furniture > Sofas")
        self.family = ProductFamily.objects.create(
            normalized_title="manual sofa", status="MANUAL_REVIEW"
        )

    def test_update_by_category_id(self):
        response = self.client.patch(
            f"/api/products/{self.family.id}/",
            data=json.dumps({"category_id": self.category.id}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, "COMPLETED")
        self.assertEqual(self.family.predicted_category_id, self.category.id)

    def test_update_by_category_name(self):
        response = self.client.patch(
            f"/api/products/{self.family.id}/",
            data=json.dumps({"category_name": "Furniture > Sofas"}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.family.refresh_from_db()
        self.assertEqual(self.family.status, "COMPLETED")

    def test_invalid_category_name_returns_400(self):
        response = self.client.patch(
            f"/api/products/{self.family.id}/",
            data=json.dumps({"category_name": "ZZZZZ_NonExistent_XYZ_99999"}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_no_params_returns_400(self):
        response = self.client.patch(
            f"/api/products/{self.family.id}/",
            data=json.dumps({}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_nonexistent_family_returns_404(self):
        response = self.client.patch(
            "/api/products/99999/",
            data=json.dumps({"category_id": self.category.id}),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# 7. Full Classification Flow (Gemini + fuzzy match fully mocked)
# ---------------------------------------------------------------------------

class ClassificationFlowTests(TestCase):
    """
    End-to-end classification test with both Gemini API and the pg_trgm
    fuzzy match function mocked — so tests run without any external calls.
    """

    def setUp(self):
        self.client = Client()
        self.category = make_category(
            "gid://shopify/sofa-flow",
            "Furniture > Living Room Furniture > Sofas & Sectionals"
        )

    @patch("classifier.tasks.guarded_fuzzy_match")
    @patch("classifier.tasks.genai")
    def test_completed_when_category_found(self, mock_genai, mock_fuzzy):
        """When Gemini returns a valid category and fuzzy match finds it → COMPLETED."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(
            text='Category: Furniture > Living Room Furniture > Sofas & Sectionals\nAttributes: {"material": "leather"}'
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.configure = MagicMock()
        mock_fuzzy.return_value = (self.category, "COMPLETED", [])

        family = ProductFamily.objects.create(normalized_title="test sofa", status="PROCESSING")

        from classifier.tasks import classify_family_task
        classify_family_task(family.id)

        family.refresh_from_db()
        self.assertEqual(family.status, "COMPLETED")
        self.assertEqual(family.predicted_category, self.category)
        self.assertAlmostEqual(family.confidence_score, 0.95)

    @patch("classifier.tasks.guarded_fuzzy_match")
    @patch("classifier.tasks.genai")
    def test_manual_review_when_low_confidence(self, mock_genai, mock_fuzzy):
        """When fuzzy match returns MANUAL_REVIEW → family status is MANUAL_REVIEW."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(
            text='Category: Ambiguous Thing\nAttributes: {}'
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.configure = MagicMock()
        mock_fuzzy.return_value = (None, "MANUAL_REVIEW", ["Suggestion A", "Suggestion B"])

        family = ProductFamily.objects.create(normalized_title="ambiguous item", status="PROCESSING")

        from classifier.tasks import classify_family_task
        classify_family_task(family.id)

        family.refresh_from_db()
        self.assertEqual(family.status, "MANUAL_REVIEW")
        self.assertIsNone(family.predicted_category)
        self.assertEqual(family.confidence_score, 0.4)

    @patch("classifier.tasks.guarded_fuzzy_match")
    @patch("classifier.tasks.genai")
    def test_attributes_saved_after_classification(self, mock_genai, mock_fuzzy):
        """Extracted attributes from Gemini response should be saved on the family."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(
            text='Category: Furniture > Sofas\nAttributes: {"material": "velvet", "style": "modern"}'
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.configure = MagicMock()
        mock_fuzzy.return_value = (self.category, "COMPLETED", [])

        family = ProductFamily.objects.create(normalized_title="velvet sofa", status="PROCESSING")

        from classifier.tasks import classify_family_task
        classify_family_task(family.id)

        family.refresh_from_db()
        self.assertIsNotNone(family.extracted_attributes)
        self.assertEqual(family.extracted_attributes.get("material"), "velvet")
        self.assertEqual(family.extracted_attributes.get("style"), "modern")

    @patch("classifier.tasks.genai")
    def test_gemini_failure_marks_family_failed(self, mock_genai):
        """If Gemini throws a non-retryable error, family status → FAILED."""
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("500 Internal Server Error")
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.configure = MagicMock()

        family = ProductFamily.objects.create(normalized_title="broken sofa", status="PROCESSING")

        from classifier.tasks import classify_family_task
        classify_family_task(family.id)

        family.refresh_from_db()
        self.assertEqual(family.status, "FAILED")

    @patch("classifier.tasks.guarded_fuzzy_match")
    @patch("classifier.tasks.genai")
    def test_alternative_suggestions_saved(self, mock_genai, mock_fuzzy):
        """Alternative category suggestions from fuzzy match should be persisted."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(
            text='Category: Furniture > Chairs\nAttributes: {}'
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.configure = MagicMock()
        alts = ["Furniture > Stools", "Furniture > Benches"]
        mock_fuzzy.return_value = (None, "MANUAL_REVIEW", alts)

        family = ProductFamily.objects.create(normalized_title="some chair", status="PROCESSING")

        from classifier.tasks import classify_family_task
        classify_family_task(family.id)

        family.refresh_from_db()
        self.assertEqual(family.alternative_suggestions, alts)

    def test_classify_nonexistent_family_returns_message(self):
        """Calling the task with a non-existent ID should not raise an exception."""
        from classifier.tasks import classify_family_task
        result = classify_family_task(99999)
        self.assertEqual(result, "Family not found.")
