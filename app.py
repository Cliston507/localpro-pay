"""LocalPro Pay: AI-checked milestone payments for seniors hiring local contractors.

Roles (each gets its own secret link):
  homeowner   - plans the job, holds money for each step in PayPal, can cancel a hold
  contractor  - uploads a proof photo when a step is done
  helper      - a trusted family member; the ONLY person who can release money
"""
import hmac
import os
import secrets
from decimal import Decimal, InvalidOperation

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import ai
import paypal_client
from store import InvalidTransition, Store

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_JOB_CENTS = 10_000_00


def _image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic", "heic"
    return None, None


def _cents(value):
    try:
        d = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    if d != d.quantize(Decimal("0.01")) or d <= 0:
        return None
    return int(d * 100)


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_PHOTO_BYTES + 64 * 1024,
        DATABASE_PATH=os.environ.get("DATABASE_PATH", os.path.join(HERE, "localpro.db")),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", os.path.join(HERE, "uploads")),
        PUBLIC_URL=os.environ.get("PUBLIC_URL", "").rstrip("/"),
    )
    if config:
        app.config.update(config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    store = Store(app.config["DATABASE_PATH"])
    paypal = app.config.get("PAYPAL_CLIENT") or paypal_client.from_env()
    app.extensions["store"] = store
    app.extensions["paypal"] = paypal

    def base_url():
        return app.config["PUBLIC_URL"] or request.host_url.rstrip("/")

    def fail(message, code=400):
        return jsonify({"error": message}), code

    def role_for(job, key):
        if not key:
            return None
        for role, field in (("homeowner", "owner_key"), ("contractor", "contractor_key"),
                            ("helper", "helper_key")):
            if hmac.compare_digest(str(key), job[field]):
                return role
        return None

    def load(milestone_id, allowed_roles):
        """Fetch milestone + job and check the caller's link. Returns (m, job, role) or error."""
        m = store.get_milestone(milestone_id)
        if not m:
            return None, None, None, fail("That step doesn't exist.", 404)
        job = store.get_job(m["job_id"])
        key = request.headers.get("X-Job-Key") or request.args.get("key") or \
            (request.get_json(silent=True) or {}).get("key") or request.form.get("key")
        role = role_for(job, key)
        if role is None:
            return None, None, None, fail("This link isn't valid for this job.", 403)
        if role not in allowed_roles:
            return None, None, None, fail(f"Only the {' or '.join(allowed_roles)} can do that.", 403)
        return m, job, role, None

    def public_job(job, role):
        out = {k: job[k] for k in ("id", "title", "description", "homeowner", "contractor",
                                   "helper", "total_cents", "safety_tips", "events")}
        out["role"] = role
        out["milestones"] = [{k: m[k] for k in ("id", "position", "title", "proof_needed",
                                                "amount_cents", "status", "ai_verdict")}
                             | {"has_photo": bool(m["photo_file"]),
                                "paypal": {"order_id": m["order_id"],
                                           "authorization_id": m["authorization_id"],
                                           "capture_id": m["capture_id"]}}
                             for m in job["milestones"]]
        if role == "homeowner":
            b = base_url()
            out["links"] = {
                "homeowner": f"{b}/?job={job['id']}&key={job['owner_key']}",
                "contractor": f"{b}/?job={job['id']}&key={job['contractor_key']}",
                "helper": f"{b}/?job={job['id']}&key={job['helper_key']}",
            }
        return out

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(HERE, "index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mock": isinstance(paypal, paypal_client.MockPayPalClient),
                "gemini": ai._use_gemini()}

    # ------------------------------------------------------------- jobs
    @app.post("/api/jobs")
    def create_job():
        data = request.get_json(silent=True) or {}
        fields = {f: str(data.get(f) or "").strip()
                  for f in ("title", "description", "homeowner", "contractor", "helper")}
        missing = [f for f, v in fields.items() if not v]
        if missing:
            return fail("Please fill in: " + ", ".join(missing) + ".")
        if any(len(v) > 1000 for v in fields.values()):
            return fail("One of the answers is too long.")
        total = _cents(data.get("total"))
        if total is None or total < 100 or total > MAX_JOB_CENTS:
            return fail("Enter a job price between $1 and $10,000.")
        milestones, tips = ai.plan_job(f"{fields['title']}. {fields['description']}", total)
        job = store.create_job(fields["title"], fields["description"], fields["homeowner"],
                               fields["contractor"], fields["helper"], total, milestones, tips)
        return jsonify(public_job(job, "homeowner")), 201

    @app.get("/api/jobs/<int:job_id>")
    def get_job(job_id):
        job = store.get_job(job_id)
        if not job:
            return fail("Job not found.", 404)
        role = role_for(job, request.headers.get("X-Job-Key") or request.args.get("key"))
        if role is None:
            return fail("This link isn't valid for this job.", 403)
        return jsonify(public_job(job, role))

    # ------------------------------------------------------------- money: hold
    @app.post("/api/milestones/<int:mid>/hold")
    def hold(mid):
        m, job, role, err = load(mid, ["homeowner"])
        if err:
            return err
        if m["status"] not in ("planned", "pending_approval"):
            return fail("Money for this step is already held or finished.", 409)
        b = base_url()
        try:
            order_id, approve_url = paypal.create_hold_order(
                m["amount_cents"], f"{job['title']} - step {m['position']}: {m['title']}",
                f"job{job['id']}-step{m['position']}",
                return_url=f"{b}/paypal/return?m={mid}",
                cancel_url=f"{b}/paypal/cancel?m={mid}")
        except paypal_client.PayPalError as exc:
            return fail(str(exc), 502)
        store.update_milestone(mid, "pending_approval" if m["status"] == "planned" else None,
                               order_id=order_id)
        return jsonify({"approve_url": approve_url})

    def _owner_page(job):
        return redirect(f"/?job={job['id']}&key={job['owner_key']}")

    @app.get("/paypal/return")
    def paypal_return():
        m = store.get_milestone(request.args.get("m", type=int) or 0)
        order_id = request.args.get("token", "")
        if not m or not order_id or m["order_id"] != order_id:
            return "That PayPal link doesn't match a step.", 400
        job = store.get_job(m["job_id"])
        if m["status"] == "held":  # page refreshed; already done
            return _owner_page(job)
        try:
            auth_id = paypal.authorize_order(order_id)
        except paypal_client.PayPalError as exc:
            store.log(job["id"], "PayPal", f"Step {m['position']}: hold failed. {exc}")
            return _owner_page(job)
        store.update_milestone(m["id"], "held", authorization_id=auth_id)
        store.log(job["id"], job["homeowner"],
                  f"Held ${m['amount_cents'] / 100:,.2f} in PayPal for step {m['position']}.")
        return _owner_page(job)

    @app.get("/paypal/cancel")
    def paypal_cancel():
        m = store.get_milestone(request.args.get("m", type=int) or 0)
        if not m:
            return "Unknown step.", 400
        job = store.get_job(m["job_id"])
        if m["status"] == "pending_approval":
            store.update_milestone(m["id"], "planned", order_id=None)
        return _owner_page(job)

    # ------------------------------------------------------------- proof photo
    @app.post("/api/milestones/<int:mid>/photo")
    def upload_photo(mid):
        m, job, role, err = load(mid, ["contractor"])
        if err:
            return err
        if m["status"] not in ("held", "submitted"):
            return fail("The homeowner needs to hold money for this step first.", 409)
        f = request.files.get("photo")
        if not f:
            return fail("Please choose a photo.")
        data = f.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            return fail("That photo is too big (8 MB max).", 413)
        mime, ext = _image_type(data)
        if not mime:
            return fail("Please send a JPG, PNG, WEBP or HEIC photo.")
        name = f"m{mid}-{secrets.token_hex(8)}.{ext}"
        with open(os.path.join(app.config["UPLOAD_DIR"], name), "wb") as out:
            out.write(data)
        verdict = ai.inspect_photo(m["title"], m["proof_needed"], data, mime)
        store.update_milestone(mid, "submitted", photo_file=name, ai_verdict=verdict)
        label = "looks done" if verdict["result"] == "looks_done" else "needs a closer look"
        store.log(job["id"], job["contractor"], f"Sent a proof photo for step {m['position']}.")
        store.log(job["id"], "AI check", f"Step {m['position']} {label}: {verdict['summary']}")
        return jsonify({"ai_verdict": verdict})

    @app.get("/api/milestones/<int:mid>/photo")
    def get_photo(mid):
        m, job, role, err = load(mid, ["homeowner", "contractor", "helper"])
        if err:
            return err
        if not m["photo_file"]:
            return fail("No photo yet.", 404)
        path = os.path.join(app.config["UPLOAD_DIR"], os.path.basename(m["photo_file"]))
        if not os.path.exists(path):
            return fail("Photo missing.", 404)
        return send_file(path, max_age=0)

    # ------------------------------------------------------------- money: release / cancel
    @app.post("/api/milestones/<int:mid>/release")
    def release(mid):
        m, job, role, err = load(mid, ["helper"])
        if err:
            return err
        if m["status"] != "submitted":
            return fail("The contractor needs to send a proof photo first.", 409)
        # Safety rail: if the AI flagged the photo, the helper must say they checked it themselves.
        verdict = m["ai_verdict"] or {}
        flagged = verdict.get("result") != "looks_done"
        body = request.get_json(silent=True) or {}
        if flagged and body.get("checked_photo") is not True:
            return fail("The AI flagged this photo. Please confirm you looked at it "
                        "yourself before releasing the money.", 409)
        try:
            capture_id = paypal.capture(m["authorization_id"])
        except paypal_client.PayPalError as exc:
            return fail(str(exc), 502)
        store.update_milestone(mid, "released", capture_id=capture_id)
        note = " after checking the flagged photo themselves" if flagged else ""
        store.log(job["id"], job["helper"],
                  f"Released ${m['amount_cents'] / 100:,.2f} for step {m['position']}{note}.")
        return jsonify(public_job(store.get_job(job["id"]), role))

    @app.post("/api/milestones/<int:mid>/cancel")
    def cancel(mid):
        m, job, role, err = load(mid, ["homeowner", "helper"])
        if err:
            return err
        if m["status"] not in ("held", "submitted"):
            return fail("There's no money held for this step.", 409)
        try:
            paypal.void(m["authorization_id"])
        except paypal_client.PayPalError as exc:
            return fail(str(exc), 502)
        store.update_milestone(mid, "voided")
        who = job["helper"] if role == "helper" else job["homeowner"]
        store.log(job["id"], who, f"Cancelled the hold for step {m['position']}. "
                                  f"{job['homeowner']} keeps the money.")
        return jsonify(public_job(store.get_job(job["id"]), role))

    @app.errorhandler(InvalidTransition)
    def bad_transition(exc):
        return fail(str(exc), 409)

    @app.errorhandler(413)
    def too_big(_exc):
        return fail("That photo is too big (8 MB max).", 413)

    return app
