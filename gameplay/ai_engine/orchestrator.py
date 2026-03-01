# gameplay/ai_engine/orchestrator.py

from dataclasses import dataclass
from django.utils import timezone

from gameplay.models import ScenarioSnapshot, StageSnapshot, DebriefSnapshot


@dataclass
class OrchestratorResult:
    validation_status: str
    error_log: dict | None = None


class AIOrchestrator:
    """
    Coordinates:
    - calling provider
    - validating output (later)
    - saving snapshots
    """

    def __init__(self, provider):
        self.provider = provider

    def generate_scenario_skeleton(self, session, incident_type: str, difficulty: int):
        """
        Returns ScenarioSnapshot
        """
        payload = self.provider.generate_scenario_skeleton(
            incident_type=incident_type,
            difficulty=difficulty,
        )

        # Minimal deterministic seed (provider should include this)
        seed = int(payload.get("seed", 0)) or int(timezone.now().timestamp())

        snap = ScenarioSnapshot.objects.create(
            session=session,
            topic=incident_type,
            difficulty=difficulty,
            seed=seed,
            scenario_json=payload,
            provider=getattr(self.provider, "name", "mock"),
            model_name=getattr(self.provider, "model_name", ""),
            validation_status="pass",
        )
        return snap

    def generate_stage_inject(
        self,
        session,
        incident_type: str,
        stage_name: str,
        seed: int,
        risk_level: int,
        question_difficulty: int,
        performance_context: dict,
    ):
        """
        Returns StageSnapshot
        """
        payload = self.provider.generate_stage_inject(
            incident_type=incident_type,
            stage_name=stage_name,
            seed=seed,
            risk_level=risk_level,
            question_difficulty=question_difficulty,
            performance_context=performance_context,
        )

        snap = StageSnapshot.objects.create(
            session=session,
            stage=stage_name,
            inject_json=payload,
            provider=getattr(self.provider, "name", "mock"),
            model_name=getattr(self.provider, "model_name", ""),
            validation_status="pass",
        )
        return snap

    def generate_debrief(self, session, incident_type: str, session_summary: dict):
        """
        Returns DebriefSnapshot
        """
        payload = self.provider.generate_debrief(
            incident_type=incident_type,
            session_summary=session_summary,
        )

        snap = DebriefSnapshot.objects.create(
            session=session,
            debrief_json=payload,
            provider=getattr(self.provider, "name", "mock"),
            model_name=getattr(self.provider, "model_name", ""),
            validation_status="pass",
        )
        return snap
