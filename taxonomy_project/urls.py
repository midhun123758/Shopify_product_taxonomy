from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('classifier.urls')),
    
    # OpenAPI Schema (The raw JSON)
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    
    # Swagger UI (The beautiful interactive webpage)
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]