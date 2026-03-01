from django.urls import path
from gameplay import views

urlpatterns = [
    path("health/", views.health, name="health"),

    # DB-based start endpoint (your new one)
    path("session/start/", views.session_start, name="session_start"),

    # Existing session endpoints
    path("session/<int:session_id>/current/", views.current_state, name="current_state"),
    path("session/<int:session_id>/answer/", views.submit_answer, name="submit_answer"),
    path("session/<int:session_id>/quit/", views.quit_session, name="quit_session"),
    path("sessions/history/", views.history, name="history"),

    # AI engine endpoints (add trailing slashes for consistency)
    path("ai/session/start/", views.AISessionStartView.as_view(), name="ai_session_start"),
    path("ai/session/<int:session_id>/stage/generate/", views.AIStageGenerateView.as_view(), name="ai_stage_generate"),
    path("ai/session/<int:session_id>/debrief/", views.AIDebriefGenerateView.as_view(), name="ai_debrief"),
    path("ai/session/<int:session_id>/current/", views.AICurrentQuestionView.as_view(), name="ai_current"),
    path("ai/session/<int:session_id>/answer/", views.AIAnswerSubmitView.as_view(), name="ai_answer"),
]