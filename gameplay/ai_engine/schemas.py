# gameplay/ai_engine/schemas.py

INCIDENT_TYPES = ["ransomware", "data_loss", "phishing", "insider_threat"]

STAGE_ORDER = ["prepare", "detect", "analyse", "remediate", "post_incident"]

ALLOWED_SCORE_DELTAS = [-10, -5, 0, 5, 10]
