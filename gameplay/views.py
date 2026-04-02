import json
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView

from .models import GameSession, Answer, StageRun, QuestionRun
from .serializers import (
    GameSessionSerializer,
    AnswerSerializer,
    StartSessionSerializer,
    GenerateStageSerializer,
)

from .services import start_ai_session, generate_ai_stage, generate_ai_debrief

from rest_framework.decorators import api_view
from gameplay.services import start_static_session

from .services import generate_ai_training_feedback
from .services import generate_ai_training_feedback, generate_ai_inject_question

from django.db.models import Max
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from gameplay.models import GameSession

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def session_start(request):

    difficulty = request.data.get("difficulty")
    topic = request.data.get("topic") or request.data.get("playbook")
    questions_per_stage = int(request.data.get("questions_per_stage", 2))

    if not difficulty or not topic:
        return Response(
            {"detail": "Missing difficulty or topic."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        session = start_static_session(
            user=request.user,
            difficulty=difficulty,
            topic=topic,
            questions_per_stage=questions_per_stage,
        )

        return Response(
            {
                "session_id": session.id,
                "status": session.status,
                "difficulty": session.difficulty,
                "topic": session.topic,
            },
            status=status.HTTP_201_CREATED,
        )

    except Exception as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
# -----------------------------
# Scenario helpers (STATIC JSON)
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


def load_scenario(topic: str) -> dict:
    scenario_path = BASE_DIR / "scenarios" / "data" / f"{topic}.json"
    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {scenario_path}")
    return json.loads(scenario_path.read_text(encoding="utf-8"))


def get_stage_and_question(scn: dict, stage_index: int, question_index: int):
    """
    Returns (stage_obj, question_obj) or (None, None) if out of range.
    """
    stages = scn.get("stages", [])
    if stage_index < 0 or stage_index >= len(stages):
        return None, None

    stage_obj = stages[stage_index]
    questions = stage_obj.get("questions", [])
    if question_index < 0 or question_index >= len(questions):
        return stage_obj, None

    return stage_obj, questions[question_index]


def build_next_payload(stage_obj: dict, question_obj: dict) -> dict:
    return {
        "stage": stage_obj.get("stage"),
        "time_limit_sec": stage_obj.get("time_limit_sec", 30),
        "question": question_obj,
    }


def advance_pointer(scn: dict, session: GameSession) -> None:
    """
    Advances session.current_question_index/current_stage_index to the next question.
    Does NOT save session.
    """
    stages = scn.get("stages", [])
    session.current_question_index += 1

    if 0 <= session.current_stage_index < len(stages):
        stage_questions = stages[session.current_stage_index].get("questions", [])
        if session.current_question_index >= len(stage_questions):
            session.current_stage_index += 1
            session.current_question_index = 0


# -----------------------------
# Endpoints (STATIC JSON)
# -----------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"ok": True, "service": "gameplay"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_or_resume(request):
    """
    POST body: {"topic":"data_loss"}
    Resumes the latest in-progress session for that topic, else creates a new one.
    """
    topic = request.data.get("topic")
    if not topic:
        return Response({"detail": "topic is required"}, status=status.HTTP_400_BAD_REQUEST)

    created = False

    session = (
        GameSession.objects.filter(user=request.user, topic=topic, status="in_progress")
        .order_by("-started_at")
        .first()
    )

    if not session:
        session = GameSession.objects.create(
            user=request.user,
            topic=topic,
            status="in_progress",
        )
        created = True

    try:
        scn = load_scenario(topic)
    except FileNotFoundError as e:
        return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

    stage_obj, question_obj = get_stage_and_question(
        scn, session.current_stage_index, session.current_question_index
    )

    payload = {
        "message": "started" if created else "resumed",
        "session": GameSessionSerializer(session).data,
        "next": build_next_payload(stage_obj, question_obj) if (stage_obj and question_obj) else None,
    }
    return Response(payload, status=status.HTTP_200_OK)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_state(request, session_id: int):

    session = GameSession.objects.filter(id=session_id, user=request.user).first()
    if not session:
        return Response({"detail": "session not found"}, status=404)

    if session.status != "in_progress":
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None
        })

    # Get active stage
    stage_run = session.stages.filter(status="active").order_by("order").first()

    if not stage_run:
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None
        })

    # Get next pending question
    qrun = stage_run.questions.filter(status="pending").order_by("order").first()

    if not qrun:
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None
        })

    return Response({
        "session": GameSessionSerializer(session).data,
        "next": {
            "stage": stage_run.stage,
            "time_limit_sec": qrun.time_limit_seconds,
            "question": {
                "id": qrun.question_key,
                "text": qrun.prompt,
                "options": qrun.choices,
            }
        }
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_answer(request, session_id: int):

    question_id = request.data.get("question_id")
    selected_choice_id = request.data.get("selected_choice_id")

    if not question_id or not selected_choice_id:
        return Response(
            {"detail": "question_id and selected_choice_id are required"},
            status=400,
        )

    session = GameSession.objects.filter(id=session_id, user=request.user).first()
    if not session:
        return Response({"detail": "session not found"}, status=404)

    if session.status != "in_progress":
        return Response({"detail": f"session is {session.status}"}, status=400)

    # Get question run
    qrun = QuestionRun.objects.select_related("stage_run").filter(
        stage_run__session=session,
        question_key=question_id,
        status="pending",
    ).first()

    if not qrun:
        return Response({"detail": "question not found or already answered"}, status=404)

    # Find selected option
    score_delta = None
    selected_text = ""

    for opt in qrun.choices:
        if str(opt.get("id")) == str(selected_choice_id):
            score_delta = int(opt.get("delta_score", 0))
            selected_text = opt.get("text", "")
            break

    if score_delta is None:
        return Response({"detail": "selected choice not found"}, status=400)

    is_correct = score_delta > 0

    with transaction.atomic():

        session = GameSession.objects.select_for_update().get(id=session.id)
        qrun = QuestionRun.objects.select_for_update().get(id=qrun.id)
        stage_run = qrun.stage_run

        # Mark question answered
        qrun.status = "answered"
        qrun.save(update_fields=["status"])

        # Create answer record
        ans = Answer.objects.create(
            session=session,
            question_run=qrun,
            selected_choice_id=str(selected_choice_id),
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=is_correct,
        )

        # Update scores
        session.total_score += score_delta
        stage_run.stage_score += score_delta

        if not is_correct:
            session.wrong_count += 1

        # Severity logic
        severity = "low"
        if session.wrong_count >= 4:
            severity = "critical"
        elif session.wrong_count >= 3:
            severity = "high"
        elif session.wrong_count >= 2:
            severity = "elevated"

        # Generate AI inject question only if answer is wrong
        ai_inject = None
        if not is_correct:
            ai_inject = generate_ai_inject_question(
                topic=session.topic,
                severity=severity,
            )

        # Advance stage if no pending questions left
        if not stage_run.questions.filter(status="pending").exists():
            stage_run.status = "done"
            stage_run.save(update_fields=["status"])

            next_stage = (
                session.stages
                .filter(status="locked")
                .order_by("order")
                .first()
            )

            if next_stage:
                next_stage.status = "active"
                next_stage.save(update_fields=["status"])
            else:
                session.status = "completed"
                session.ended_reason = "finished"
                session.ended_at = timezone.now()

        # Fail condition
        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_reason = "too_many_wrongs"
            session.ended_at = timezone.now()

        stage_run.save(update_fields=["stage_score"])
        session.save()

    return Response(
        {
            "answer": AnswerSerializer(ans).data,
            "session": GameSessionSerializer(session).data,
            "severity": severity,
            "ai_inject": ai_inject,
        },
        status=201,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def quit_session(request, session_id):
    session = GameSession.objects.filter(id=session_id, user=request.user).first()

    if not session:
        return Response({"detail": "session not found"}, status=404)

    if session.status == "in_progress":
        session.status = "abandoned"
        session.ended_reason = "user_quit"
        session.ended_at = timezone.now()
        session.advice_summary = generate_ai_training_feedback(session)
        session.save(update_fields=["status", "ended_reason", "ended_at", "advice_summary"])

    return Response({
        "session": GameSessionSerializer(session).data,
        "training_feedback": session.advice_summary,
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def history(request):
    qs = GameSession.objects.filter(user=request.user).order_by("-started_at")[:50]
    return Response({"sessions": GameSessionSerializer(qs, many=True).data})

# -----------------------------
# Endpoints (AI Engine)
# -----------------------------
class AISessionStartView(APIView):
    """
    POST /api/gameplay/ai/session/start
    body: { "difficulty": 3, "incident_type": "data_loss" }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = StartSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        difficulty = serializer.validated_data["difficulty"]
        incident_type = serializer.validated_data["incident_type"]

        # start_ai_session expects topic (your DB field), so map incident_type -> topic
        session, scenario_snapshot = start_ai_session(
            user=request.user,
            topic=incident_type,
            difficulty=difficulty,
        )

        return Response(
            {
                "session_id": session.id,
                "incident_type": incident_type,
                "difficulty": difficulty,
                "scenario": scenario_snapshot.scenario_json,
            },
            status=201,
        )



class AIStageGenerateView(APIView):
    """
    POST /api/gameplay/ai/session/<session_id>/stage/generate
    body: { "stage_name": "prepare" }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id: int):
        session = get_object_or_404(GameSession, id=session_id, user=request.user)

        serializer = GenerateStageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        stage_name = serializer.validated_data["stage_name"]

        stage_snapshot = generate_ai_stage(session=session, stage_name=stage_name)

        return Response(
            {
                "session_id": session.id,
                "stage_name": stage_name,
                "stage_inject": stage_snapshot.inject_json,
                "validation_status": stage_snapshot.validation_status,
            },
            status=200,
        )


class AIDebriefGenerateView(APIView):
    """
    POST /api/gameplay/ai/session/<session_id>/debrief
    body: {}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id: int):
        session = get_object_or_404(GameSession, id=session_id, user=request.user)

        debrief_snapshot = generate_ai_debrief(session=session)

        return Response(
            {
                "session_id": session.id,
                "debrief": debrief_snapshot.debrief_json,
                "validation_status": debrief_snapshot.validation_status,
            },
            status=200,
        )

class AICurrentQuestionView(APIView):
    """
    GET /api/gameplay/ai/session/<session_id>/current
    Returns the next pending QuestionRun in the active stage.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id: int):
        session = get_object_or_404(GameSession, id=session_id, user=request.user)

        # Earliest active stage
        stage_run = session.stages.filter(status="active").order_by("order").first()
        if not stage_run:
            return Response({"session_id": session.id, "next": None}, status=200)

        # Next pending question
        qrun = stage_run.questions.filter(status="pending").order_by("order").first()
        if not qrun:
            return Response({"session_id": session.id, "next": None}, status=200)

        base_time = qrun.time_limit_seconds or 30

        # Dynamic timer reduction based on pressure
        adjusted_time = max(
            8,
            base_time - (session.pressure_level // 20)
        )

        escalation_level = None

        if session.pressure_level >= 85:
            escalation_level = "critical"
        elif session.pressure_level >= 60:
            escalation_level = "high"
        elif session.pressure_level >= 40:
            escalation_level = "elevated"

        return Response(
            {
                "escalation_level": escalation_level,
                "session_id": session.id,
                "stage": stage_run.stage,
                "time_limit_sec": adjusted_time,
                "pressure_level": session.pressure_level,
                "question": {
                    "id": qrun.question_key,
                    "text": qrun.prompt,
                    "options": qrun.choices,
                },
            },
            status=200,
        )


        # Base time comes from QuestionRun (already stored)
        base_time = qrun.time_limit_seconds or 30

        # Dynamic reduction based on pressure
        adjusted_time = max(
            8,  # never allow below 8 seconds
            base_time - (session.pressure_level // 20)
        )

        return Response(
            {
                "session_id": session.id,
                "stage": stage_run.stage,
                "time_limit_sec": adjusted_time,
                "pressure_level": session.pressure_level,
                "question": {
                    "id": qrun.question_key,
                    "text": qrun.prompt,
                    "options": qrun.choices,
                },
            },
            status=200,
        )






class AIAnswerSubmitView(APIView):
    """
    POST /api/gameplay/ai/session/<session_id>/answer
    body: { "question_id": "...", "selected_choice_id": "a" }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id: int):
        session = get_object_or_404(GameSession, id=session_id, user=request.user)

        question_id = request.data.get("question_id")
        selected_choice_id = request.data.get("selected_choice_id")

        if not question_id or not selected_choice_id:
            return Response(
                {"detail": "question_id and selected_choice_id are required"},
                status=400,
            )

        qrun = QuestionRun.objects.select_related("stage_run").filter(
            stage_run__session=session,
            question_key=question_id,
        ).first()

        if not qrun:
            return Response({"detail": "question not found"}, status=404)

        if hasattr(qrun, "answer"):
            return Response({"detail": "already answered"}, status=409)

        # Score lookup from stored snapshot choices
        score_delta = None
        selected_text = ""
        for opt in (qrun.choices or []):
            if str(opt.get("id")) == str(selected_choice_id):
                score_delta = int(opt.get("score", 0))
                selected_text = opt.get("text", "")
                break

        if score_delta is None:
            return Response({"detail": "selected choice not found"}, status=400)

        is_correct = score_delta > 0

        with transaction.atomic():
            # lock rows
            session = GameSession.objects.select_for_update().get(id=session.id)
            qrun = QuestionRun.objects.select_for_update().select_related("stage_run").get(id=qrun.id)
            stage_run = qrun.stage_run

            # Mark question answered
            qrun.status = "answered"
            qrun.save(update_fields=["status"])

            # Create answer record
            ans = Answer.objects.create(
                session=session,
                question_run=qrun,
                selected_choice_id=str(selected_choice_id),
                selected_text=selected_text,
                score_delta=score_delta,
                is_correct=is_correct,
            )

            # Update scores
            session.total_score += score_delta
            stage_run.stage_score += score_delta

            if not is_correct:
                session.wrong_count += 1

            # ----------------------
            # Pressure update logic
            # ----------------------
            if score_delta > 0:
                session.pressure_level = max(0, session.pressure_level - 5)
            elif score_delta == -5:
                session.pressure_level += 10
            elif score_delta <= -10:
                session.pressure_level += 20


        # WAR MODE: harsher penalties under pressure
        if session.pressure_level > 50:
            if score_delta == -5:
                score_delta = -8
            elif score_delta <= -10:
                score_delta = -15

            # clamp 0–100
            session.pressure_level = min(session.pressure_level, 100)

            # Fail by pressure
            if session.pressure_level >= 100:
                session.status = "failed"
                session.ended_reason = "system_escalation"
                session.ended_at = timezone.now()

            # Fail by wrong answers
            elif session.wrong_count >= session.wrong_limit:
                session.status = "failed"
                session.ended_reason = "too_many_wrongs"
                session.ended_at = timezone.now()

            # save stage + session
            stage_run.save(update_fields=["stage_score"])
            session.save(update_fields=[
                "total_score",
                "wrong_count",
                "pressure_level",
                "status",
                "ended_reason",
                "ended_at",
            ])

            # ----------------------
            # Auto-progression
            # ----------------------
            if session.status != "failed":

                # If no pending questions remain in this stage
                if not stage_run.questions.filter(status="pending").exists():
                    stage_run.status = "done"
                    stage_run.save(update_fields=["status"])

                    # Activate next locked stage
                    next_stage = (
                        session.stages
                        .filter(status="locked")
                        .order_by("order")
                        .first()
                    )

                    if next_stage:
                        next_stage.status = "active"
                        next_stage.save(update_fields=["status"])
                    else:
                        # No more stages → complete session
                        session.status = "completed"
                        session.ended_reason = "finished"
                        session.ended_at = timezone.now()
                        session.save(update_fields=["status", "ended_reason", "ended_at"])

           

            # If failed, stop here
            if session.status == "failed":
                return Response(
                    {
                        "answer": AnswerSerializer(ans).data,
                        "session": GameSessionSerializer(session).data,
                        "next": None,
                    },
                    status=201,
                )

        return Response(
            {
                "answer": AnswerSerializer(ans).data,
                "session": GameSessionSerializer(session).data,
            },
            status=201,
        )
    
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def leaderboard_view(request):
    leaderboard_data = (
        GameSession.objects
        .filter(user__isnull=False)
        .values("user__username")
        .annotate(points=Max("total_score"))
        .order_by("-points")[:3]
    )

    results = []
    for i, row in enumerate(leaderboard_data, start=1):
        results.append({
            "rank": i,
            "name": row["user__username"],
            "points": row["points"]
        })

    return Response(results)