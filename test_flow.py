"""End-to-end tests with fake PayPal and no AI calls. Run: pytest -q"""
import io
from urllib.parse import parse_qs, urlparse

import pytest

import ai
from app import create_app
from paypal_client import MockPayPalClient

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALPRO_MOCK", "1")
    pp = MockPayPalClient()
    app = create_app({"DATABASE_PATH": str(tmp_path / "t.db"),
                      "UPLOAD_DIR": str(tmp_path / "up"), "PAYPAL_CLIENT": pp})
    return app.test_client(), pp


def new_job(client, total="2400"):
    r = client.post("/api/jobs", json={
        "title": "Roof leak", "description": "Replace shingles over kitchen", "total": total,
        "homeowner": "Margaret", "contractor": "Coastline Roofing", "helper": "Dana"})
    assert r.status_code == 201, r.json
    job = r.json
    keys = {role: parse_qs(urlparse(url).query)["key"][0] for role, url in job["links"].items()}
    return job, keys


def hold(client, mid, owner_key):
    r = client.post(f"/api/milestones/{mid}/hold", headers={"X-Job-Key": owner_key})
    assert r.status_code == 200, r.json
    back = urlparse(r.json["approve_url"])
    r = client.get(f"{back.path}?{back.query}")  # PayPal sends the homeowner back
    assert r.status_code == 302


def test_full_happy_path(env):
    client, pp = env
    job, keys = new_job(client)
    steps = job["milestones"]
    assert sum(m["amount_cents"] for m in steps) == 240000
    assert all(m["amount_cents"] <= 120000 for m in steps)

    m = steps[0]
    hold(client, m["id"], keys["homeowner"])
    r = client.get(f"/api/jobs/{job['id']}", headers={"X-Job-Key": keys["contractor"]})
    assert r.json["role"] == "contractor" and "links" not in r.json
    assert r.json["milestones"][0]["status"] == "held"

    r = client.post(f"/api/milestones/{m['id']}/photo", headers={"X-Job-Key": keys["contractor"]},
                    data={"photo": (io.BytesIO(JPEG), "roof.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    assert r.json["ai_verdict"]["advisory_only"] is True

    r = client.post(f"/api/milestones/{m['id']}/release", headers={"X-Job-Key": keys["helper"]})
    assert r.status_code == 200, r.json
    assert r.json["milestones"][0]["status"] == "released"
    assert [c[0] for c in pp.calls] == ["create", "authorize", "capture"]

    # Double release is refused and never captures twice.
    r = client.post(f"/api/milestones/{m['id']}/release", headers={"X-Job-Key": keys["helper"]})
    assert r.status_code == 409
    assert [c[0] for c in pp.calls].count("capture") == 1


def test_only_helper_can_release(env):
    client, _ = env
    job, keys = new_job(client)
    m = job["milestones"][0]
    hold(client, m["id"], keys["homeowner"])
    client.post(f"/api/milestones/{m['id']}/photo", headers={"X-Job-Key": keys["contractor"]},
                data={"photo": (io.BytesIO(JPEG), "a.jpg")}, content_type="multipart/form-data")
    for role in ("homeowner", "contractor"):
        r = client.post(f"/api/milestones/{m['id']}/release", headers={"X-Job-Key": keys[role]})
        assert r.status_code == 403
    r = client.post(f"/api/milestones/{m['id']}/release", headers={"X-Job-Key": "wrong"})
    assert r.status_code == 403


def test_cannot_release_without_photo(env):
    client, _ = env
    job, keys = new_job(client)
    m = job["milestones"][0]
    hold(client, m["id"], keys["homeowner"])
    r = client.post(f"/api/milestones/{m['id']}/release", headers={"X-Job-Key": keys["helper"]})
    assert r.status_code == 409


def test_cancel_hold_keeps_money(env):
    client, pp = env
    job, keys = new_job(client)
    m = job["milestones"][0]
    hold(client, m["id"], keys["homeowner"])
    r = client.post(f"/api/milestones/{m['id']}/cancel", headers={"X-Job-Key": keys["helper"]})
    assert r.status_code == 200
    assert r.json["milestones"][0]["status"] == "voided"
    assert pp.calls[-1][0] == "void"
    r = client.post(f"/api/milestones/{m['id']}/photo", headers={"X-Job-Key": keys["contractor"]},
                    data={"photo": (io.BytesIO(JPEG), "a.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 409


def test_rejects_non_image_and_bad_input(env):
    client, _ = env
    job, keys = new_job(client)
    m = job["milestones"][0]
    hold(client, m["id"], keys["homeowner"])
    r = client.post(f"/api/milestones/{m['id']}/photo", headers={"X-Job-Key": keys["contractor"]},
                    data={"photo": (io.BytesIO(b"<script>"), "x.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert client.post("/api/jobs", json={"title": "x"}).status_code == 400
    r = client.post("/api/jobs", json={"title": "a", "description": "b", "total": "-5",
                                        "homeowner": "a", "contractor": "b", "helper": "c"})
    assert r.status_code == 400


def test_paypal_return_must_match_order(env):
    client, _ = env
    job, keys = new_job(client)
    m = job["milestones"][0]
    client.post(f"/api/milestones/{m['id']}/hold", headers={"X-Job-Key": keys["homeowner"]})
    r = client.get(f"/paypal/return?m={m['id']}&token=FAKE-ORDER")
    assert r.status_code == 400


@pytest.mark.parametrize("raw,total", [
    ({"milestones": [{"title": "All", "share": 1.0}, {"title": "x", "share": 0}]}, 240000),
    ({"milestones": [{"title": "a", "share": 90}, {"title": "b", "share": 5},
                     {"title": "c", "share": 5}]}, 99999),
    ({"milestones": []}, 1001),
    ({"milestones": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}, 100),
    ({"garbage": True}, 500000),
])
def test_plan_rails(raw, total):
    steps, tips = ai.enforce_plan(raw, total)
    assert 2 <= len(steps) <= 5
    assert sum(s["amount_cents"] for s in steps) == total
    assert max(s["amount_cents"] for s in steps) <= total // 2 + 1
    assert tips[0] == "Never pay the whole job up front."


def test_low_confidence_photo_needs_review():
    v = ai.enforce_verdict({"looks_done": True, "confidence": 0.4, "summary": "Maybe."})
    assert v["result"] == "needs_review"
    v = ai.enforce_verdict({"looks_done": True, "confidence": 0.95, "summary": "Yes."})
    assert v["result"] == "looks_done"


def test_first_step_is_small_even_if_ai_asks_for_half():
    raw = {"milestones": [{"title": "Start", "share": 0.5}, {"title": "Finish", "share": 0.5}]}
    steps, _ = ai.enforce_plan(raw, 240000)
    assert len(steps) >= 3
    assert sum(s["amount_cents"] for s in steps) == 240000
    assert steps[0]["amount_cents"] <= 60000
    assert all(s["amount_cents"] <= 120000 for s in steps)

    raw = {"milestones": [{"title": "A", "share": 0.9}, {"title": "B", "share": 0.05},
                          {"title": "C", "share": 0.05}]}
    for total in (240000, 100001, 99, 333333):
        steps, _ = ai.enforce_plan(raw, total)
        assert sum(s["amount_cents"] for s in steps) == total
        assert steps[0]["amount_cents"] <= int(total * 0.25)
        assert all(s["amount_cents"] <= int(total * 0.5) for s in steps)
