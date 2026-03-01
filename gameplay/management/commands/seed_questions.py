import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gameplay.models import Playbook, Question, Option


class Command(BaseCommand):
    help = "Seed Playbooks/Questions/Options from JSON files in gameplay/seed_data/"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            type=str,
            default=None,
            help="Optional path to seed_data directory. Defaults to gameplay/seed_data/",
        )
        parser.add_argument(
            "--wipe",
            action="store_true",
            help="Dangerous: deletes all Playbook/Question/Option rows before importing.",
        )

    def handle(self, *args, **options):
        seed_dir = options["dir"]

        if seed_dir:
            seed_path = Path(seed_dir).resolve()
        else:
            # gameplay/management/commands/seed_questions.py -> gameplay/seed_data/
            seed_path = Path(__file__).resolve().parents[2] / "seed_data"

        if not seed_path.exists() or not seed_path.is_dir():
            raise CommandError(f"seed_data directory not found: {seed_path}")

        json_files = sorted(seed_path.glob("*.json"))
        if not json_files:
            raise CommandError(f"No .json files found in: {seed_path}")

        if options["wipe"]:
            self.stdout.write(self.style.WARNING("WIPING all Playbooks/Questions/Options..."))
            Option.objects.all().delete()
            Question.objects.all().delete()
            Playbook.objects.all().delete()

        totals = {"files": 0, "playbooks": 0, "questions": 0, "options": 0}

        for fp in json_files:
            totals["files"] += 1
            with open(fp, "r", encoding="utf-8") as f:
                payload = json.load(f)

            self._validate_payload(payload, fp.name)

            version = payload.get("version", 1)
            slug = payload["playbook"]
            difficulty = payload["difficulty"]
            stage = payload.get("stage", 1)

            with transaction.atomic():
                playbook, created_pb = Playbook.objects.update_or_create(
                    slug=slug,
                    difficulty=difficulty,
                    version=version,
                    defaults={"stage": stage},
                )
                if created_pb:
                    totals["playbooks"] += 1

                for q in payload["questions"]:
                    q_obj, created_q = Question.objects.update_or_create(
                        external_id=q["external_id"],
                        defaults={
                            "playbook": playbook,
                            "phase": q["phase"],
                            "prompt": q["prompt"],
                            "is_active": bool(q.get("is_active", True)),
                        },
                    )
                    if created_q:
                        totals["questions"] += 1

                    for opt in q["options"]:
                        _, created_o = Option.objects.update_or_create(
                            question=q_obj,
                            label=opt["label"],
                            defaults={
                                "text": opt["text"],
                                "delta_score": int(opt["delta_score"]),
                            },
                        )
                        if created_o:
                            totals["options"] += 1

            self.stdout.write(self.style.SUCCESS(f"Imported: {fp.name}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("✅ Seeding complete"))
        self.stdout.write(f"Files processed: {totals['files']}")
        self.stdout.write(f"Playbooks created: {totals['playbooks']}")
        self.stdout.write(f"Questions created: {totals['questions']}")
        self.stdout.write(f"Options created: {totals['options']}")

    def _validate_payload(self, payload, filename):
        required_top = ["playbook", "difficulty", "questions"]
        for k in required_top:
            if k not in payload:
                raise CommandError(f"[{filename}] Missing top-level key: {k}")

        if payload["difficulty"] not in {"easy", "medium", "hard"}:
            raise CommandError(
                f"[{filename}] Invalid difficulty '{payload['difficulty']}'. Use: easy/medium/hard"
            )

        if not isinstance(payload["questions"], list) or len(payload["questions"]) == 0:
            raise CommandError(f"[{filename}] 'questions' must be a non-empty list")

        for i, q in enumerate(payload["questions"], start=1):
            for k in ["external_id", "phase", "prompt", "options"]:
                if k not in q:
                    raise CommandError(f"[{filename}] Q{i} missing key: {k}")

            if q["phase"] not in {"Prepare", "Detect", "Analyse", "Remediation", "Post-Incident"}:
                raise CommandError(f"[{filename}] Q{i} invalid phase: {q['phase']}")

            if not isinstance(q["options"], list) or len(q["options"]) < 2:
                raise CommandError(f"[{filename}] Q{i} options must be a list (>=2)")

            seen_labels = set()
            for opt in q["options"]:
                for k in ["label", "text", "delta_score"]:
                    if k not in opt:
                        raise CommandError(f"[{filename}] Q{i} option missing key: {k}")
                if opt["label"] in seen_labels:
                    raise CommandError(f"[{filename}] Q{i} duplicate option label: {opt['label']}")
                seen_labels.add(opt["label"])