import os
from celery import Celery

# Tell Celery to use our Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxonomy_project.settings')

app = Celery('taxonomy_project')

# Load task modules from all registered Django apps
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# NEW: We MUST include these lines so it connects to the Docker Redis!
app.conf.broker_url = 'redis://redis:6379/0'
app.conf.result_backend = 'django-db'