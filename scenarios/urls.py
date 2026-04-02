from django.urls import path
from .views import topics, scenario_detail
from django.urls import path
from .views import leaderboard_view

urlpatterns = [
    path('leaderboard/', leaderboard_view, name='leaderboard'),
]

urlpatterns = [
    path("topics/", topics, name="topics"),
    path("scenario/<str:topic>/", scenario_detail, name="scenario_detail"),
]
