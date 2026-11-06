# LocalPro Pay

**AI-checked, step-by-step payments so seniors never pay a contractor for work that isn't done.**

Built for the PayPal AI Hackathon. Flask + PayPal Orders v2 (authorize / capture / void) + Google Gemini.

## The problem

Older adults lose billions every year to home-repair scams. The usual pattern is a big deposit up front and work that never gets finished. A homeowner living alone has no easy way to check the work, and family members who live far away can't help in time.

## How LocalPro Pay works

1. **AI payment plan.** The homeowner describes the job and the quoted price. Gemini splits it into 2–4 steps that can each be checked with a photo. Code (not the AI) makes sure the steps add up to the exact price and that **no step is more than 50%**.
2. **Money is held, not paid.** For each step the homeowner approves a PayPal order with `intent=AUTHORIZE`. The money is *held* on her account. The contractor can see it's real, but can't touch it.
3. **Proof photo + AI check.** The contractor uploads a photo. Gemini checks it against the proof the homeowner asked for and explains the result in plain words, which the app can read aloud. If the AI isn't confident, the step is marked "needs a closer look".
4. **A trusted person releases the money.** Only the **trusted helper** (a daughter, a son, a friend) can release a payment, which captures the authorization. A scammer who pressures the homeowner still can't get paid. The homeowner or the helper can cancel a hold at any time, which voids it so she keeps the money.

The AI gives advice only. It can never move money.

## Demo story

Margaret, 78, lives alone on the Oregon coast. Her roof leaks over the kitchen and a roofer quotes $2,400. Her daughter Dana lives three hours away.

1. Margaret describes the leak. LocalPro Pay makes a two-step plan and warns her never to pay the whole job up front.
2. She holds $1,200 for step 1 in PayPal.
3. The roofer sends a photo of the new shingles. The AI checks it and reads the result aloud.
4. Dana opens her link, looks at the photo, and taps **Release**. The roofer is paid for exactly the work that's done.

If the photo had been blurry or unrelated, the AI would have flagged it, and Dana could cancel the hold.

## Run it

```bash
pip install -r requirements-dev.txt
LOCALPRO_MOCK=1 python wsgi.py        # offline demo: fake PayPal, no AI key needed
pytest -q                              # 12 end-to-end tests
```

For real Sandbox payments, copy `env.example` to `.env` (or set the variables on your host) and fill in:

| Variable | Where to get it |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | developer.paypal.com → Apps & Credentials → Sandbox |
| `GEMINI_API_KEY` | aistudio.google.com/apikey |

### Deploy on Render

New → Blueprint → pick this repo. `render.yaml` sets everything up; paste the three keys when asked. Pay with a **Sandbox personal account** (developer.paypal.com → Testing Tools → Sandbox Accounts).

Note: the free plan uses temporary storage, so jobs reset when the service restarts. That's fine for a demo.

## Files

| File | What it does |
|---|---|
| `app.py` | Routes, role checks (homeowner / contractor / helper links), payment flow |
| `paypal_client.py` | PayPal Orders v2: create AUTHORIZE order, authorize, capture, void, with idempotency keys |
| `ai.py` | Gemini planner and photo inspector, plus the code safety rails |
| `store.py` | SQLite storage, with a strict status machine so money can't skip a step |
| `index.html` | Single-page UI: large text, high contrast, read-aloud |
| `test_flow.py` | End-to-end tests with a fake PayPal |

## Safety by design

- Each role gets its own secret link; only the helper's link can release money
- Status changes are enforced (`planned → held → submitted → released / voided`), so there are no double payments
- Every PayPal call carries a `PayPal-Request-Id`, so retries never move money twice
- Uploads are checked by file signature (JPG / PNG / WEBP / HEIC, 8 MB max)

## What's next

- Pay the contractor's own PayPal account directly with PayPal multiparty payments
- Re-authorize long jobs automatically (PayPal authorizations last up to 29 days)
- SMS alerts to the trusted helper, and Spanish-language support
- Before-and-after photo comparison and contractor license checks

## License

MIT, see [LICENSE](LICENSE).
