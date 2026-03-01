from django.contrib import admin
from django.urls import path, include
from .auth_views import login_view, refresh_view
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("gameplay.urls")),

    # Cookie-based auth
    path("api/auth/login/", login_view),
    path("api/auth/refresh/", refresh_view),
    path("api/gameplay/", include("gameplay.urls")),
    path("api/", include("scenarios.urls")),
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]