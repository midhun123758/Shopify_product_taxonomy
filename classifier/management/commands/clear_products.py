from django.core.management.base import BaseCommand
from classifier.models import Product, ProductFamily
import redis
from django.conf import settings

class Command(BaseCommand):
    help = 'Clears all current Product data and flushes the Redis task queue.'

    def handle(self, *args, **options):
        self.stdout.write("Deleting all Product and ProductFamily records from database...")
        
        product_count, _ = Product.objects.all().delete()
        family_count, _ = ProductFamily.objects.all().delete()
        
        self.stdout.write(self.style.SUCCESS(f"Deleted {product_count} products and {family_count} product families."))

        self.stdout.write("Flushing Redis Celery queue...")
        try:
            # Try connecting to Docker redis host first, fallback to localhost
            try:
                r = redis.Redis(host='redis', port=6379, db=0)
                r.flushdb()
            except Exception:
                r = redis.Redis(host='localhost', port=6379, db=0)
                r.flushdb()
            self.stdout.write(self.style.SUCCESS("Successfully flushed Redis task queue and DB."))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Could not connect to Redis: {e}"))

        self.stdout.write(self.style.SUCCESS("Cleanup completed! All product data and queues are cleared."))
