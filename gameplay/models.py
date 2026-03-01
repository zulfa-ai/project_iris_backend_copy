from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator


class GameSession(models.Model):
    STATUS_CHOICES = [
        ("created", "Created"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("abandoned", "Abandoned"),
    ]

    DIFFICULTY_CHOICES = [
        ("easy", "Easy"),
        ("medium", "Medium"),
        ("critical", "Critical"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="game_sessions",
    )

    topic = models.CharField(max_length=50)

    current_stage_index = models.PositiveIntegerField(default=0)
    current_question_index = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    ended_reason = models.CharField(max_length=50, null=True, blank=True)
    last_activity_at = models.DateTimeField(auto_now=True)

    total_score = models.IntegerField(default=0)

    wrong_count = models.IntegerField(default=0)
    wrong_limit = models.IntegerField(default=5)

    advice_summary = models.TextField(blank=True, default="")

    # pressure + difficulty
    pressure_level = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    difficulty = models.CharField(
        max_length=20,
        choices=DIFFICULTY_CHOICES,
        default="medium"
    )

    factors = models.JSONField(default=dict, blank=True)

    def end(self, status: str, reason: str | None = None):
        self.status = status
        self.ended_reason = reason
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_reason", "ended_at"])

    def __str__(self):
        return f"{self.user} - {self.topic} - {self.status}"


class StageRun(models.Model):
    STAGES = [
        ("prepare", "Prepare"),
        ("detect", "Detect"),
        ("analyse", "Analyse"),
        ("remediate", "Remediate"),
        ("post_incident", "Post-Incident"),
    ]
    STATUS = [("locked", "Locked"), ("active", "Active"), ("done", "Done")]

    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="stages")
    stage = models.CharField(max_length=30, choices=STAGES)
    order = models.PositiveIntegerField()  # 0..4
    status = models.CharField(max_length=10, choices=STATUS, default="locked")

    stage_score = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "stage"], name="unique_stage_per_session"),
            models.UniqueConstraint(fields=["session", "order"], name="unique_stage_order_per_session"),
        ]

    def __str__(self):
        return f"{self.session.id} {self.stage} ({self.status})"


class QuestionRun(models.Model):
    STATUS = [("pending", "Pending"), ("answered", "Answered"), ("skipped", "Skipped")]

    stage_run = models.ForeignKey(StageRun, on_delete=models.CASCADE, related_name="questions")

    # Stable IDs that can refer to template questions now, AI later (optional)
    question_key = models.CharField(max_length=80)  # e.g. "prep-1" or "ai-uuid-123"

    # Snapshot of what the user saw (VERY important for AI & audit)
    prompt = models.TextField()
    choices = models.JSONField(default=list)  # [{id,label,is_correct?,points?}, ...]

    order = models.PositiveIntegerField()  # 0..N in that stage
    status = models.CharField(max_length=10, choices=STATUS, default="pending")

    time_limit_seconds = models.PositiveIntegerField(default=30, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stage_run", "order"], name="unique_question_order_per_stage_run"),
            models.UniqueConstraint(fields=["stage_run", "question_key"], name="unique_question_key_per_stage_run"),
        ]

    def __str__(self):
        return f"{self.stage_run.id} {self.question_key} ({self.status})"


class Answer(models.Model):
    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="answers")
    question_run = models.OneToOneField(QuestionRun, on_delete=models.CASCADE, related_name="answer")

    # Store stable choice id instead of text (text can change)
    selected_choice_id = models.CharField(max_length=80)
    selected_text = models.CharField(max_length=200, blank=True, default="")

    score_delta = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)

    # Idempotency for double-click / retries (highly recommended)
    client_answer_id = models.UUIDField(null=True, blank=True, unique=True)

    answered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.session.id} {self.question_run.question_key} {self.score_delta}"
    
# ---------------------
# AI Snapshot Models 
# ---------------------

class ScenarioSnapshot(models.Model):
    """
    Stores the AI-generated scenario skeleton (or full scenario) for a session.
    One snapshot per session.
    """
    session = models.OneToOneField(
        GameSession, on_delete=models.CASCADE, related_name="scenario_snapshot"
    )

    # Keep these as simple metadata (because your GameSession uses `topic`)
    topic = models.CharField(max_length=50)          # same as session.topic (e.g. "data_loss")
    difficulty = models.PositiveIntegerField(default=3)
    seed = models.IntegerField()

    scenario_json = models.JSONField()  # skeleton JSON, validated

    created_at = models.DateTimeField(auto_now_add=True)

    # Audit fields (optional but very useful)
    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    prompt_hash = models.CharField(max_length=64, blank=True, default="")
    validation_status = models.CharField(max_length=32, default="pass")  # pass/fallback/repaired
    error_log = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"ScenarioSnapshot(session={self.session.id}, topic={self.topic})"


class StageSnapshot(models.Model):
    """
    Stores the AI-generated stage inject (questions/options/scoring) per stage.
    One snapshot per session + stage.
    """
    session = models.ForeignKey(
        GameSession, on_delete=models.CASCADE, related_name="stage_snapshots"
    )

    stage = models.CharField(max_length=30, choices=StageRun.STAGES)  # enforce allowed stage names
    inject_json = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)

    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    prompt_hash = models.CharField(max_length=64, blank=True, default="")
    validation_status = models.CharField(max_length=32, default="pass")
    error_log = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "stage"], name="unique_stage_snapshot_per_session")
        ]

    def __str__(self):
        return f"StageSnapshot(session={self.session.id}, stage={self.stage})"


class DebriefSnapshot(models.Model):
    """
    Stores the AI-generated end-of-session debrief.
    One snapshot per session.
    """
    session = models.OneToOneField(
        GameSession, on_delete=models.CASCADE, related_name="debrief_snapshot"
    )

    debrief_json = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    prompt_hash = models.CharField(max_length=64, blank=True, default="")
    validation_status = models.CharField(max_length=32, default="pass")
    error_log = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"DebriefSnapshot(session={self.session.id})"


# ---------------------
# Game Models
# ---------------------
class Playbook(models.Model):
    DIFFICULTY_CHOICES = [
        ("easy", "Easy"),
        ("medium", "Medium"),
        ("hard", "Hard"),
    ]

    slug = models.SlugField(max_length=50)  # e.g. "phishing", "ransomware"
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES)
    version = models.PositiveIntegerField(default=1)
    stage = models.PositiveIntegerField(default=1)  # optional, can keep for reference

    class Meta:
        unique_together = ("slug", "difficulty", "version")

    def __str__(self):
        return f"{self.slug} ({self.difficulty}) v{self.version}"


class Question(models.Model):
    PHASE_CHOICES = [
        ("Prepare", "Prepare"),
        ("Detect", "Detect"),
        ("Analyse", "Analyse"),
        ("Remediation", "Remediation"),
        ("Post-Incident", "Post-Incident"),
    ]

    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE, related_name="questions")
    external_id = models.CharField(max_length=120, unique=True)  # key for idempotent seeding
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES)
    prompt = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["playbook", "phase"]),
            models.Index(fields=["external_id"]),
        ]

    def __str__(self):
        return f"{self.external_id}"


class Option(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    label = models.CharField(max_length=1)  # A/B/C
    text = models.TextField()
    delta_score = models.IntegerField()

    class Meta:
        unique_together = ("question", "label")

    def __str__(self):
        return f"{self.question.external_id}:{self.label}"