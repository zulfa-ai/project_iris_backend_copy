from django.contrib import admin
from django.urls import path, include
from iris_backend.auth_views import login_view, refresh_view
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),

    # Your apps
    path("api/", include("gameplay.urls")),
    path("api/", include("scenarios.urls")),

    # Auth
    path("api/auth/login/", login_view),
    path("api/auth/refresh/", refresh_view),

    # JWT
    path("api/token/", TokenObtainPairView.as_view()),
    path("api/token/refresh/", TokenRefreshView.as_view()),

    path("admin/", admin.site.urls),

    path("api/gameplay/", include("gameplay.urls")),
    path("api/", include("scenarios.urls")),
]