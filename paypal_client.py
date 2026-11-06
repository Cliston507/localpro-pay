"""PayPal Orders v2 client: hold money (authorize), release it (capture), or cancel the hold (void).

LocalPro Pay never takes the homeowner's money up front. Each milestone is its own
PayPal order created with intent=AUTHORIZE, so the funds are only *held* on the
homeowner's PayPal account until a trusted helper approves the finished work.

Set LOCALPRO_MOCK=1 to run without PayPal (tests and offline demos).
"""
import os
import time
import uuid

import requests

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(Exception):
    pass


def _money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


class PayPalClient:
    def __init__(self, client_id, client_secret, env="sandbox", timeout=20):
        if not client_id or not client_secret:
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required.")
        self.base = LIVE if env == "live" else SANDBOX
        self.auth = (client_id, client_secret)
        self.timeout = timeout
        self._token = None
        self._token_expires = 0.0

    def _access_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        r = requests.post(f"{self.base}/v1/oauth2/token", auth=self.auth,
                          data={"grant_type": "client_credentials"}, timeout=self.timeout)
        if r.status_code != 200:
            raise PayPalError(f"PayPal login failed ({r.status_code}). Check your Sandbox keys.")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _post(self, path, json_body=None, request_id=None):
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            # Idempotency: retrying the same request never moves money twice.
            "PayPal-Request-Id": request_id or str(uuid.uuid4()),
            "Prefer": "return=representation",
        }
        r = requests.post(f"{self.base}{path}", json=json_body or {}, headers=headers,
                          timeout=self.timeout)
        if r.status_code >= 400:
            detail = ""
            try:
                err = r.json()
                detail = err.get("message") or err.get("name") or ""
                issues = [d.get("issue") for d in err.get("details", []) if d.get("issue")]
                if issues:
                    detail += f" ({', '.join(issues)})"
            except ValueError:
                detail = r.text[:200]
            raise PayPalError(f"PayPal said no ({r.status_code}): {detail}")
        return r.json() if r.content else {}

    def create_hold_order(self, amount_cents, description, reference, return_url, cancel_url):
        """Create an AUTHORIZE order. Returns (order_id, approve_url)."""
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference,
                "description": description[:127],
                "amount": {"currency_code": "USD", "value": _money(amount_cents)},
            }],
            # Classic checkout context: the most reliable flow in PayPal Sandbox,
            # including on phones and in-app browsers.
            "application_context": {
                "shipping_preference": "NO_SHIPPING",
                "user_action": "CONTINUE",
                "landing_page": "LOGIN",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        order = self._post("/v2/checkout/orders", body, request_id=f"create-{reference}-{uuid.uuid4()}")
        approve = next((l["href"] for l in order.get("links", [])
                        if l.get("rel") in ("approve", "payer-action")), None)
        if not approve:
            raise PayPalError("PayPal didn't return an approval link.")
        return order["id"], approve

    def authorize_order(self, order_id):
        """After the homeowner approves, place the hold. Returns the authorization id."""
        order = self._post(f"/v2/checkout/orders/{order_id}/authorize",
                           request_id=f"authorize-{order_id}")
        try:
            auth = order["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            raise PayPalError("PayPal didn't return an authorization.")
        if auth.get("status") not in ("CREATED", "PENDING"):
            raise PayPalError(f"Hold not placed (status {auth.get('status')}).")
        return auth["id"]

    def capture(self, authorization_id):
        """Release the held money to the business account. Returns the capture id."""
        cap = self._post(f"/v2/payments/authorizations/{authorization_id}/capture",
                         {"final_capture": True}, request_id=f"capture-{authorization_id}")
        if cap.get("status") not in ("COMPLETED", "PENDING"):
            raise PayPalError(f"Payment not released (status {cap.get('status')}).")
        return cap["id"]

    def void(self, authorization_id):
        """Cancel the hold so the homeowner keeps the money."""
        self._post(f"/v2/payments/authorizations/{authorization_id}/void",
                   request_id=f"void-{authorization_id}")


class MockPayPalClient:
    """Offline stand-in with the same interface. Approval is instant."""

    def __init__(self):
        self.calls = []

    def create_hold_order(self, amount_cents, description, reference, return_url, cancel_url):
        order_id = f"MOCK-ORDER-{uuid.uuid4().hex[:10].upper()}"
        self.calls.append(("create", order_id, amount_cents))
        sep = "&" if "?" in return_url else "?"
        return order_id, f"{return_url}{sep}token={order_id}&PayerID=MOCKPAYER"

    def authorize_order(self, order_id):
        self.calls.append(("authorize", order_id))
        return f"MOCK-AUTH-{order_id[-10:]}"

    def capture(self, authorization_id):
        self.calls.append(("capture", authorization_id))
        return f"MOCK-CAPTURE-{authorization_id[-10:]}"

    def void(self, authorization_id):
        self.calls.append(("void", authorization_id))


def from_env():
    if os.environ.get("LOCALPRO_MOCK") == "1":
        return MockPayPalClient()
    return PayPalClient(os.environ.get("PAYPAL_CLIENT_ID"),
                        os.environ.get("PAYPAL_CLIENT_SECRET"),
                        os.environ.get("PAYPAL_ENV", "sandbox"))
