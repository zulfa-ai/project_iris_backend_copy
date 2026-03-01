from gameplay.models import QuestionRun


def inject_to_questionruns(stage_run, inject_json: dict) -> int:
    """
    Convert AI stage inject JSON into QuestionRun rows.

    inject_json expected shape:
    {
      "stage": "prepare",
      "time_limit_sec": 30,
      "questions": [
        {"id":"prepare-q1","text":"...","options":[{"id":"a","text":"..","score":10}, ...]}
      ]
    }

    Returns number of QuestionRun rows created.
    """

    questions = inject_json.get("questions", [])
    time_limit = int(inject_json.get("time_limit_sec", 30))

    created = 0

    for idx, q in enumerate(questions):
        question_key = q.get("id") or f"{stage_run.stage}-q{idx+1}"
        prompt = q.get("text", "")

        # Normalize choices into your stored format
        # keep scores inside choices for deterministic scoring later
        raw_opts = q.get("options", [])
        choices = []
        for opt in raw_opts:
            choices.append(
                {
                    "id": opt.get("id", opt.get("text", "")),
                    "text": opt.get("text", ""),
                    "score": int(opt.get("score", 0)),
                }
            )

        # Idempotent create (avoid duplicates if generate called twice)
        obj, was_created = QuestionRun.objects.get_or_create(
            stage_run=stage_run,
            question_key=question_key,
            defaults={
                "prompt": prompt,
                "choices": choices,
                "order": idx,
                "status": "pending",
                "time_limit_seconds": time_limit,
            },
        )

        # If it already existed, we don't overwrite (audit safety)
        if was_created:
            created += 1

    return created
