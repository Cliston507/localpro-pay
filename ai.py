"""Gemini job planner and photo inspector, wrapped in code-enforced safety rails.

The AI only *advises*. It never moves money. Whatever the model says, the code below:
  * splits the job into 3-5 steps whose amounts add up exactly to the agreed total,
  * caps the first step at 25% (a small start, not half the job) and every other step at 50%,
  * downgrades a photo check to "needs a closer look" unless the model is confident.
Without GEMINI_API_KEY (or with LOCALPRO_MOCK=1) a simple built-in planner is used.
"""
import base64
import json
import os

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_STEP_SHARE = 0.5
FIRST_STEP_SHARE = 0.25  # the up-front step stays small
MIN_STEPS = 3
MIN_CONFIDENCE = 0.7

ALWAYS_TIPS = [
    "Never pay the whole job up front.",
    "Only release money after you or your helper has seen proof the step is done.",
    "Get the contractor's license number and check it with your state.",
]


class AIError(Exception):
    pass


def _use_gemini():
    return bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("LOCALPRO_MOCK") != "1"


def _gemini(parts, timeout=40):
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    r = requests.post(
        GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                 "Content-Type": "application/json"},
        json={"contents": [{"role": "user", "parts": parts}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "temperature": 0.2}},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise AIError(f"Gemini error {r.status_code}: {r.text[:200]}")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, ValueError) as exc:
        raise AIError(f"Gemini returned something unexpected: {exc}")


# ---------------------------------------------------------------- planning
def _fallback_plan(description, total_cents):
    steps = [
        ("Materials delivered and work started",
         "Photo of the materials on site and the work area prepared.", 0.2),
        ("Main work done",
         "Photo showing most of the repair finished.", 0.4),
        ("Job finished and cleaned up",
         "Photo of the finished work and the area cleaned up.", 0.4),
    ]
    return {"milestones": [{"title": t, "proof_needed": p, "share": w} for t, p, w in steps],
            "safety_tips": []}


def enforce_plan(raw, total_cents):
    """Turn whatever the model returned into a safe, exact payment plan."""
    items = [m for m in (raw.get("milestones") or []) if isinstance(m, dict)][:5]
    if len(items) < MIN_STEPS:
        items = _fallback_plan("", total_cents)["milestones"]

    weights = []
    for m in items:
        w = m.get("share", m.get("percent"))
        try:
            w = float(w)
            if w > 1:
                w /= 100.0
        except (TypeError, ValueError):
            w = 0.0
        weights.append(max(w, 0.0))
    if sum(weights) <= 0:
        weights = [1.0] * len(items)
    total_w = sum(weights)
    shares = [w / total_w for w in weights]

    # Cap the first step at 25% and the rest at 50%; spread any excess over steps with room.
    caps = [FIRST_STEP_SHARE] + [MAX_STEP_SHARE] * (len(shares) - 1)
    for _ in range(20):
        over = [i for i, s in enumerate(shares) if s > caps[i] + 1e-9]
        if not over:
            break
        excess = sum(shares[i] - caps[i] for i in over)
        for i in over:
            shares[i] = caps[i]
        room = [i for i, s in enumerate(shares) if s < caps[i] - 1e-9]
        if not room:
            break
        room_total = sum(shares[i] for i in room)
        for i in room:
            part = shares[i] / room_total if room_total > 0 else 1 / len(room)
            shares[i] += excess * part

    amounts = [int(total_cents * s) for s in shares]
    amounts[-1] += total_cents - sum(amounts)  # exact to the cent
    cap_cents = [int(total_cents * c) for c in caps]
    # Rounding can push a step a cent over its cap; move it to a step that has room.
    for i, a in enumerate(amounts):
        if a > cap_cents[i]:
            extra = a - cap_cents[i]
            amounts[i] = cap_cents[i]
            j = max(range(len(amounts)), key=lambda k: cap_cents[k] - amounts[k])
            amounts[j] += extra

    milestones = []
    for m, cents in zip(items, amounts):
        milestones.append({
            "title": str(m.get("title") or "Step")[:120],
            "proof_needed": str(m.get("proof_needed") or m.get("proof") or
                                "Clear photo of the finished step.")[:300],
            "amount_cents": cents,
        })
    tips = [str(t)[:200] for t in (raw.get("safety_tips") or []) if t][:3]
    return milestones, ALWAYS_TIPS + [t for t in tips if t not in ALWAYS_TIPS]


def plan_job(description, total_cents):
    raw = None
    if _use_gemini():
        prompt = (
            "You help seniors pay home-repair contractors safely, step by step.\n"
            f"Job: {description}\nAgreed total: ${total_cents / 100:.2f}\n"
            "Split the job into 3 to 5 milestones a homeowner can verify from a photo. "
            "The first milestone must be 25% of the total or less, and no milestone may be "
            "more than half the total. Use plain, friendly words.\n"
            'Reply as JSON: {"milestones":[{"title":str,"proof_needed":str,'
            '"share":number between 0 and 1}],"safety_tips":[str, up to 3 scam warnings '
            "specific to this kind of job]}"
        )
        try:
            raw = _gemini([{"text": prompt}])
        except (AIError, requests.RequestException):
            raw = None
    if not isinstance(raw, dict):
        raw = _fallback_plan(description, total_cents)
    return enforce_plan(raw, total_cents)


# ---------------------------------------------------------------- inspection
def enforce_verdict(raw, proof_needed=""):
    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    looks_done = bool(raw.get("looks_done")) and confidence >= MIN_CONFIDENCE
    summary = str(raw.get("summary") or "").strip()[:400]
    concerns = [str(c)[:200] for c in (raw.get("concerns") or []) if c][:4]
    if raw.get("looks_done") and not looks_done:
        concerns.append("The AI wasn't sure enough. Please look at the photo yourself.")
    # The contractor sees their own message, written to them, not to the homeowner.
    contractor_note = str(raw.get("contractor_note") or "").strip()[:400]
    if not contractor_note:
        if looks_done:
            contractor_note = "Thanks, your photo was sent. The helper will check it and release your payment."
        elif proof_needed:
            contractor_note = f"Please send a clearer photo that shows: {proof_needed}"
        else:
            contractor_note = "Please send a clearer photo that shows this step is done."
    return {
        "result": "looks_done" if looks_done else "needs_review",
        "confidence": round(confidence, 2),
        "summary": summary or ("The photo matches this step." if looks_done
                               else "This photo needs a closer look."),
        "concerns": concerns,
        "contractor_note": contractor_note,
        "advisory_only": True,
    }


def inspect_photo(milestone_title, proof_needed, image_bytes, mime_type):
    if not _use_gemini():
        return enforce_verdict({
            "looks_done": True, "confidence": 0.9,
            "summary": "Demo mode: the photo was received. A real check uses Gemini.",
            "concerns": [],
        }, proof_needed)
    prompt = (
        "You check proof-of-work photos for a senior paying a contractor.\n"
        f"Step: {milestone_title}\nProof the homeowner asked for: {proof_needed}\n"
        "Does this photo clearly show that step is done? Be skeptical: blurry, unrelated, "
        "stock-looking or screenshot photos are NOT proof. Write the summary in two short, "
        "kind sentences a 78-year-old can understand when read aloud. The summary is for "
        "the homeowner. Also write contractor_note: one or two short sentences spoken "
        "directly to the contractor (say \"you\"), telling them what to send if the photo "
        "is not good enough, or thanking them if it is.\n"
        'Reply as JSON: {"looks_done":bool,"confidence":number 0-1,'
        '"summary":str,"contractor_note":str,"concerns":[str]}'
    )
    parts = [{"text": prompt},
             {"inline_data": {"mime_type": mime_type,
                              "data": base64.b64encode(image_bytes).decode()}}]
    try:
        raw = _gemini(parts)
    except (AIError, requests.RequestException):
        return enforce_verdict({"looks_done": False, "confidence": 0,
                                "summary": "The AI check isn't available right now.",
                                "concerns": ["Please look at the photo yourself."]},
                               proof_needed)
    return enforce_verdict(raw if isinstance(raw, dict) else {}, proof_needed)
