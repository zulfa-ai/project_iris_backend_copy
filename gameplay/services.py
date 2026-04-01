from dataclasses import asdict
from django.db import transaction
from django.utils import timezone

from .models import GameSession, Answer
from .exceptions import NotFound, Conflict, GameplayError
from .providers import BaseScenarioProvider

import random
from dataclasses import dataclass
from typing import Dict, List

from gameplay.models import Playbook, Question

import json
import requests

from .models import Answer

class SessionService:
    def __init__(self, provider: BaseScenarioProvider):
        self.provider = provider

    def start_or_resume(self, user, topic: str) -> dict:
        if not topic:
            raise GameplayError("topic is required")

        session = (
            GameSession.objects
            .filter(user=user, topic=topic, status="in_progress")
            .order_by("-started_at")
            .first()
        )
        if not session:
            session = GameSession.objects.create(user=user, topic=topic, status="in_progress")

        scn = self.provider.load(topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)
        return {
            "session_id": session.id,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "current": asdict(current) if current else None,
        }

    def current_state(self, session: GameSession) -> dict:
        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)
        return {
            "session_id": session.id,
            "topic": session.topic,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "current": asdict(current) if current else None,
        }


class AnswerService:
    def __init__(self, provider: BaseScenarioProvider):
        self.provider = provider

    @transaction.atomic
    def submit_answer(self, session: GameSession, question_id: str, selected_text: str) -> dict:
        # Lock row to prevent race conditions
        session = GameSession.objects.select_for_update().get(id=session.id)

        if session.status != "in_progress":
            raise GameplayError(f"session is {session.status}, cannot answer")

        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)

        if not current:
            session.status = "completed"
            session.ended_at = timezone.now()
            session.ended_reason = "finished"
            session.save(update_fields=["status", "ended_at", "ended_reason"])
            return {"detail": "No more questions. Session completed."}

        q = current.question
        if q.get("id") != question_id:
            raise GameplayError("question_id does not match current question")

        # conflict check (still keep your unique constraint)
        if Answer.objects.filter(session=session, question_id=question_id).exists():
            raise Conflict("already answered")

        # find score
        score_delta = None
        for opt in q.get("options", []):
            if opt.get("text") == selected_text:
                score_delta = int(opt.get("score", 0))
                break
        if score_delta is None:
            raise GameplayError("selected_text not found in options")

        # update session counters
        is_wrong = score_delta < 0
        if is_wrong:
            session.wrong_count += 1
        session.total_score += score_delta

        # advance pointer
        session.current_question_index += 1

        # handle stage boundary
        stages = scn.get("stages", [])
        if session.current_stage_index < len(stages):
            stage_questions = stages[session.current_stage_index].get("questions", [])
            if session.current_question_index >= len(stage_questions):
                session.current_stage_index += 1
                session.current_question_index = 0

        # fail condition
        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_at = timezone.now()
            session.ended_reason = "too_many_wrongs"

        session.save()

        Answer.objects.create(
            session=session,
            stage=current.stage,
            question_id=question_id,
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=(score_delta > 0),
        )

        next_current = self.provider.get_current_question(
            scn, session.current_stage_index, session.current_question_index
        )

        # finish condition
        if session.status == "in_progress" and next_current is None:
            session.status = "completed"
            session.ended_at = timezone.now()
            session.ended_reason = "finished"
            session.save(update_fields=["status", "ended_at", "ended_reason"])

        return {
            "session_id": session.id,
            "status": session.status,
            "ended_reason": session.ended_reason,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "next": next_current.__dict__ if next_current else None,
            "awarded_points": score_delta,
        }
# ============================
# AI SESSION SERVICES (model-correct)
# ============================

from gameplay.ai_engine.orchestrator import AIOrchestrator
from gameplay.ai_engine.providers.mock_provider import MockProvider
from gameplay.models import GameSession, StageRun, ScenarioSnapshot, StageSnapshot, DebriefSnapshot


def _get_ai_orchestrator():
    return AIOrchestrator(provider=MockProvider())


def start_ai_session(user, topic: str, difficulty: int):
    """
    Creates a new GameSession using your existing schema.
    incident_type == topic for now.
    """
    session = GameSession.objects.create(
        user=user,
        topic=topic,
        status="in_progress",
        total_score=0,
        wrong_count=0,
    )

    orch = _get_ai_orchestrator()
    scenario_snapshot = orch.generate_scenario_skeleton(
        session=session,
        incident_type=topic,
        difficulty=difficulty,
    )

    from gameplay.ai_engine.schemas import STAGE_ORDER

    # Pre-create ALL stages
    for i, stage in enumerate(STAGE_ORDER):
        StageRun.objects.create(
            session=session,
            stage=stage,
            order=i,
            status="active" if i == 0 else "locked",
            stage_score=0,
        )


    return session, scenario_snapshot


def generate_ai_stage(session: GameSession, stage_name: str):
    """
    Generates stage inject once per stage (resume-safe).
    Ensures QuestionRun rows exist even if snapshot already exists.
    """

    orch = _get_ai_orchestrator()

    # 1) Get or create snapshot
    stage_snapshot = session.stage_snapshots.filter(stage=stage_name).first()
    if not stage_snapshot:
        scenario_snapshot = session.scenario_snapshot
        seed = scenario_snapshot.seed
        difficulty = scenario_snapshot.difficulty

        performance_context = {
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
        }

        stage_snapshot = orch.generate_stage_inject(
            session=session,
            incident_type=session.topic,
            stage_name=stage_name,
            seed=seed,
            risk_level=difficulty,
            question_difficulty=difficulty,
            performance_context=performance_context,
        )

    # 2) Ensure StageRun exists + active
    order_map = {
        "prepare": 0,
        "detect": 1,
        "analyse": 2,
        "remediate": 3,
        "post_incident": 4,
    }

    stage_run, _ = StageRun.objects.get_or_create(
        session=session,
        stage=stage_name,
        defaults={
            "order": order_map[stage_name],
            "status": "active",
            "stage_score": 0,
        },
    )

    if stage_run.status != "active":
        stage_run.status = "active"
        stage_run.save(update_fields=["status"])

    # 3) Backfill QuestionRuns if missing
    from gameplay.ai_engine.adapters import inject_to_questionruns
    if not stage_run.questions.exists():
        inject_to_questionruns(stage_run, stage_snapshot.inject_json)

    return stage_snapshot


def generate_ai_debrief(session: GameSession):
    """
    Generates and stores debrief snapshot.
    """
    orch = _get_ai_orchestrator()

    session_summary = {
        "topic": session.topic,
        "status": session.status,
        "total_score": session.total_score,
        "wrong_count": session.wrong_count,
        "stages": list(session.stages.values("stage", "stage_score", "status")),
    }

    return orch.generate_debrief(
        session=session,
        incident_type=session.topic,
        session_summary=session_summary,
    )

PHASES_IN_ORDER = ["Prepare", "Detect", "Analyse", "Remediation", "Post-Incident"]


@dataclass(frozen=True)
class StagePack:
    phase: str
    questions: List[Question]


def pick_playbook(*, difficulty: str, playbook_slug: str, version: int = 1) -> Playbook:
    """
    Point 1: user chooses difficulty + topic (playbook).
    """
    return Playbook.objects.get(slug=playbook_slug, difficulty=difficulty, version=version)

#================================
# Static Session Builder
#================================
from gameplay.models import GameSession, StageRun, QuestionRun, Playbook, Question
from django.db import transaction
import random


STAGE_SLUG_MAP = {
    "Prepare": "prepare",
    "Detect": "detect",
    "Analyse": "analyse",
    "Remediation": "remediate",
    "Post-Incident": "post_incident",
}


@transaction.atomic
def start_static_session(user, difficulty: str, topic: str, questions_per_stage: int = 2):

    # 1️⃣ Create session
    session = GameSession.objects.create(
        user=user,
        topic=topic,
        difficulty=difficulty,
        status="in_progress",
    )

    # 2️⃣ Get playbook
    playbook = Playbook.objects.get(slug=topic, difficulty=difficulty)

    # 3️⃣ Create StageRuns
    stage_runs = {}

    for order, phase in enumerate(STAGE_SLUG_MAP.keys()):
        stage_runs[phase] = StageRun.objects.create(
            session=session,
            stage=STAGE_SLUG_MAP[phase],
            order=order,
            status="active" if order == 0 else "locked",
        )

    # 4️⃣ Pick and snapshot questions
    for phase, stage_run in stage_runs.items():

        questions = list(
            Question.objects.filter(
                playbook=playbook,
                phase=phase,
                is_active=True
            ).prefetch_related("options")
        )

        random.shuffle(questions)
        selected = questions[:questions_per_stage]

        for q_order, q in enumerate(selected):

            QuestionRun.objects.create(
                stage_run=stage_run,
                question_key=q.external_id,
                prompt=q.prompt,
                choices=[
                    {
                        "id": opt.label,
                        "label": opt.label,
                        "text": opt.text,
                        "delta_score": opt.delta_score,
                    }
                    for opt in q.options.all()
                ],
                order=q_order,
            )

    return session
    

def generate_ai_training_feedback(session):
    wrong_answers = Answer.objects.filter(
        session=session,
        is_correct=False
    ).select_related("question_run")

    # Default abandoned fallback
    if session.status == "abandoned" and not wrong_answers.exists():
        return (
            "Session abandoned before completion.\n\n"
            "General Debrief:\n"
            "- The scenario was not completed, so full performance could not be assessed.\n"
            "- The user should review core incident response steps for this topic.\n"
            "- Recommended focus areas include verification, escalation, and correct response procedures.\n"
            "- Best next step: retry the scenario and complete all stages for a full debrief."
        )

    summary_data = []

    for ans in wrong_answers:
        q = ans.question_run
        correct = None

        for opt in q.choices:
            if opt.get("delta_score", 0) > 0:
                correct = opt.get("text")
                break

        summary_data.append({
            "question": q.prompt,
            "wrong_answer": ans.selected_text,
            "correct_answer": correct
        })

    # If no wrong answers at all
    if not summary_data:
        if session.status == "abandoned":
            return (
                "Session abandoned before completion.\n\n"
                "General Debrief:\n"
                "- No incorrect answers were recorded before the session ended.\n"
                "- However, the scenario was not completed, so overall readiness cannot be fully assessed.\n"
                "- Best next step: complete the full scenario to receive a more accurate debrief."
            )

        return (
            "General Debrief:\n"
            "- Performance was strong overall.\n"
            "- No incorrect answers were recorded.\n"
            "- Recommended next step: continue with harder scenarios to test advanced incident response skills."
        )

    prompt = f"""
You are a cybersecurity training instructor.

Generate a generalised debrief for a tabletop training session.

Session status: {session.status}
Topic: {session.topic}
Total score: {session.total_score}
Wrong count: {session.wrong_count}

The debrief must:
- be concise
- be generalised, not overly detailed
- include overall performance
- include key weakness areas
- include recommended next steps
- if the session was abandoned, mention that it was incomplete

Return plain text with short bullet points.

Incorrect answers:
{json.dumps(summary_data, indent=2)}
"""

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3:latest",
                "prompt": prompt,
                "stream": False
            },
            timeout=60
        )

        response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()

    except Exception:
        lines = []

        if session.status == "abandoned":
            lines.append("General Debrief:")
            lines.append("- Session was abandoned before completion.")
        else:
            lines.append("General Debrief:")
            lines.append("- Session reached an end state.")

        lines.append(f"- Topic: {session.topic}")
        lines.append(f"- Total score: {session.total_score}")
        lines.append(f"- Incorrect answers recorded: {session.wrong_count}")

        if session.wrong_count > 0:
            lines.append("- Main improvement area: incident recognition and response decision-making.")
            lines.append("- Recommended next step: review incorrect decisions and retry the scenario.")
        else:
            lines.append("- Performance was generally positive.")
            lines.append("- Recommended next step: move to a harder difficulty.")

        return "\n".join(lines)
    
    import json
    import requests


def generate_ai_inject_question(topic: str, severity: str):
    prompt = f"""
You are generating a cybersecurity scenario injection question.

Incident type: {topic}
Severity: {severity}

Return ONLY valid JSON.
Do not include markdown fences.
Do not include explanations.

Format:
{{
  "question": "short scenario question",
  "options": [
    {{
      "id": "A",
      "text": "good response",
      "delta_score": 10
    }},
    {{
      "id": "B",
      "text": "bad response",
      "delta_score": -10
    }}
  ]
}}

Rules:
- The injection must be phrased as a question.
- There must be exactly 2 answers.
- Answer A must be the good answer with delta_score 10.
- Answer B must be the bad answer with delta_score -10.
- Keep it realistic and concise.
- Keep the question under 25 words.
- Match the incident type and severity.
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3:latest",
                "prompt": prompt,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        raw = data.get("response", "").strip()

        if raw.startswith("```json"):
            raw = raw[len("```json"):].strip()
        if raw.startswith("```"):
            raw = raw[len("```"):].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        parsed = json.loads(raw)

        if (
            isinstance(parsed, dict)
            and "question" in parsed
            and "options" in parsed
            and isinstance(parsed["options"], list)
            and len(parsed["options"]) == 2
        ):
            return parsed

    except Exception as e:
        print(f"[AI inject fallback triggered] {e}")

    # fallback
    return {
        "question": f"A {severity} {topic} escalation is developing. What should the team do next?",
        "options": [
            {
                "id": "A",
                "text": "Investigate and respond immediately",
                "delta_score": 10,
            },
            {
                "id": "B",
                "text": "Delay action and hope it resolves itself",
                "delta_score": -10,
            },
        ],
    }