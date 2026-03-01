# gameplay/ai_engine/providers/mock_provider.py

import random

class MockProvider:
    name = "mock"
    model_name = "mock-v1"

    def generate_scenario_skeleton(self, incident_type: str, difficulty: int) -> dict:
        seed = random.randint(100000, 999999)
        return {
            "seed": seed,
            "incident_type": incident_type,
            "difficulty": difficulty,
            "stages": ["prepare", "detect", "analyse", "remediate", "post_incident"],
        }

    def generate_stage_inject(
        self,
        incident_type: str,
        stage_name: str,
        seed: int,
        risk_level: int,
        question_difficulty: int,
        performance_context: dict,
    ) -> dict:
        # minimal inject format (you can expand later)
        return {
            "stage": stage_name,
            "time_limit_sec": 30,
            "questions": [
                {
                    "id": f"{stage_name}-q1",
                    "text": f"[{incident_type}] {stage_name}: What is the best next action?",
                    "options": [
                        {"id": "a", "text": "Correct action", "score": 10},
                        {"id": "b", "text": "Risky action", "score": -5},
                        {"id": "c", "text": "Wrong action", "score": -10},
                    ],
                }
            ],
        }

    def generate_debrief(self, incident_type: str, session_summary: dict) -> dict:
        return {
            "incident_type": incident_type,
            "summary": "Session completed. Review weak stages and improve response steps.",
            "session_summary": session_summary,
        }
