# ============================================================
#  BetLink Bridge API — betlink_api.py
#  Connects the Mobile App UI to the same betlink.db brain
#  shared with BetLink_bot.py.
#
#  Engine  : Flask + Flask-CORS
#  Storage : SQLite WAL mode (concurrent reads with the bot)
#  Auth    : Ethiopian phone + in-memory OTP (no extra DB table)
#  Payments: Chapa 2-step verify  +  Manual CBE/Telebirr screenshot
#
#  Run alongside the bot:
#      python betlink_api.py          # API on port 5000
#      python BetLink_bot.py          # Bot polling (separate terminal)
# ============================================================

import os
import re
import time
import uuid
import random
import sqlite3
import logging
import requests

from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  (mirrors bot constants — single source of truth)
# ─────────────────────────────────────────────────────────────
DB_PATH            = "betlink.db"          # Same file the bot uses
CHAPA_SECRET_KEY   = os.environ.get("CHAPA_SECRET_KEY",  "CHASECK_TEST-MysUHnycnxWFrB4s3XiAVLkdTDIpOdCf")
CHAPA_INIT_URL     = "https://api.chapa.co/v1/transaction/initialize"
CHAPA_VERIFY_URL   = "https://api.chapa.co/v1/transaction/verify"
LISTING_FEE_BIRR   = 200
UNLOCK_FEE_BIRR    = 100
LISTING_ACTIVE_DAYS = 30
CBE_ACCOUNT        = "1000385968397 (BetLink Platform)"
TELEBIRR_ACCOUNT   = "0920323890 (BetLink Platform)"
ADMIN_TELEGRAM_ID  = int(os.environ.get("ADMIN_TELEGRAM_ID", "1602487678"))
APP_SECRET         = os.environ.get("APP_SECRET", "Ken@123%mesfin")

ETHIOPIAN_PHONE_RE = re.compile(
    r"^(?:(?:09|07)\d{8}|(?:\+2519|\+2517)\d{8})$"
)

# ── In-memory OTP store: { normalized_phone: {"otp": "123456", "expires": ts} }
_otp_store: dict[str, dict] = {}

# ── In-memory session store: { session_token: {"app_user_id": int, "phone": str, "role": str} }
_sessions: dict[str, dict] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BetLinkAPI")

# ─────────────────────────────────────────────────────────────
#  FLASK APP
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False, allow_headers=["Authorization", "Content-Type", "X-Admin-Key"], methods=["GET", "POST", "OPTIONS"])  # Restrict to your domain in prod


# ─────────────────────────────────────────────────────────────
#  1.  DATABASE — WAL MODE + SHARED CONNECTION PER REQUEST
#
#  WAL (Write-Ahead Logging) lets the bot and the API server
#  read/write betlink.db at the exact same millisecond without
#  locking.  This block must run before ANY query executes.
# ─────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Return a per-request SQLite connection with WAL mode enabled.
    Flask's `g` object ensures one connection per HTTP request,
    automatically closed at teardown.
    """
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # ── WAL CONFIGURATION ──────────────────────────────────────
        # journal_mode=WAL  → concurrent readers never block a writer
        # synchronous=NORMAL → safe durability with WAL; faster than FULL
        # busy_timeout=5000  → wait up to 5 s instead of raising "locked"
        # cache_size=-8000   → 8 MB page cache per connection
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous  = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA cache_size   = -8000;")
        # ───────────────────────────────────────────────────────────

        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_app_tables() -> None:
    """
    Add the two extra tables the mobile app needs without touching
    any of the bot's existing tables (safe to run every startup).
    """
    with sqlite3.connect(DB_PATH) as conn:
        # Enable WAL here too so the schema migration itself is safe
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.executescript(
            """
            -- Mobile app users (phone-based; maps to bot's users table via app_user_id)
            CREATE TABLE IF NOT EXISTS app_users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                phone        TEXT    NOT NULL UNIQUE,
                role         TEXT    DEFAULT 'renter',
                created_at   TEXT    DEFAULT (datetime('now'))
            );

            -- Manual payment screenshot submissions (for admin review queue)
            CREATE TABLE IF NOT EXISTS manual_payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                app_user_id  INTEGER NOT NULL,
                house_id     TEXT,
                tx_ref       TEXT    NOT NULL UNIQUE,
                payment_type TEXT    NOT NULL,  -- 'unlock' | 'listing'
                amount_birr  INTEGER NOT NULL,
                screenshot   TEXT,              -- base64 or file path
                status       TEXT    DEFAULT 'pending',  -- pending|approved|rejected
                submitted_at TEXT    DEFAULT (datetime('now'))
            );

            -- Notifications for app users
            CREATE TABLE IF NOT EXISTS notifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                app_user_id  INTEGER NOT NULL,
                message      TEXT    NOT NULL,
                is_read      INTEGER DEFAULT 0,
                created_at   TEXT    DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()
    log.info("App tables verified / created.")


# ─────────────────────────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str | None:
    """Normalize to +251XXXXXXXXX format. Returns None if invalid."""
    p = raw.strip().replace(" ", "").replace("-", "")
    if not ETHIOPIAN_PHONE_RE.match(p):
        return None
    if p.startswith("09") or p.startswith("07"):
        return "+251" + p[1:]   # 09... → +2519...
    return p


def _generate_otp() -> str:
    return str(random.randint(100000, 999999))


def _send_otp_sms(phone: str, otp: str) -> bool:
    """
    Replace the body of this function with your SMS gateway call
    (e.g. Africas Talking, Ethio Telecom bulk SMS API).
    For now it logs the OTP — safe for development.
    """
    log.info("[OTP] %s → %s  (replace with real SMS gateway)", phone, otp)
    # Example Africas Talking integration (uncomment and configure):
    # import africastalking
    # africastalking.initialize(username="betlink", api_key=os.environ["AT_API_KEY"])
    # sms = africastalking.SMS
    # sms.send(f"BetLink code: {otp}. Valid 5 minutes.", [phone])
    return True


def _require_auth(f):
    """Decorator: validates Bearer session token, injects current_user into request."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        session = _sessions.get(token)
        if not session:
            return jsonify({"error": "Invalid or expired session token"}), 401
        request.current_user = session
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────
#  2a.  POST /api/auth/request-otp
#
#  Front-end sends the Ethiopian phone number.
#  API generates a 6-digit OTP, stores it in memory (5 min TTL),
#  and fires it via SMS.
#
#  Request body (JSON):
#    { "phone": "0911234567" }
#
#  Response:
#    200 { "message": "OTP sent", "expires_in": 300 }
#    400 { "error": "Invalid Ethiopian phone number" }
# ─────────────────────────────────────────────────────────────

@app.route("/api/auth/request-otp", methods=["POST"])
def auth_request_otp():
    data  = request.get_json(silent=True) or {}
    raw   = data.get("phone", "").strip()
    phone = _normalize_phone(raw)

    if not phone:
        return jsonify({"error": "Invalid Ethiopian phone number"}), 400

    otp     = _generate_otp()
    expires = time.time() + 300   # 5-minute window

    _otp_store[phone] = {"otp": otp, "expires": expires}

    ok = _send_otp_sms(phone, otp)
    if not ok:
        return jsonify({"error": "SMS gateway failure. Try again."}), 502

    log.info("OTP requested for %s", phone)
    return jsonify({"message": "OTP sent", "expires_in": 300}), 200


# ─────────────────────────────────────────────────────────────
#  2b.  POST /api/auth/verify-otp
#
#  Front-end submits the OTP the user received.
#  On success: upsert app_users row, issue a session token.
#
#  Request body (JSON):
#    { "phone": "0911234567", "otp": "482910" }
#
#  Response:
#    200 { "token": "<uuid>", "user": { "id": 3, "phone": "...", "role": "renter" } }
#    401 { "error": "Invalid or expired OTP" }
# ─────────────────────────────────────────────────────────────

@app.route("/api/auth/verify-otp", methods=["POST"])
def auth_verify_otp():
    data  = request.get_json(silent=True) or {}
    raw   = data.get("phone", "").strip()
    otp_submitted = str(data.get("otp", "")).strip()
    phone = _normalize_phone(raw)

    if not phone:
        return jsonify({"error": "Invalid phone number"}), 400

    stored = _otp_store.get(phone)
    if not stored:
        return jsonify({"error": "No OTP requested for this number"}), 401
    if time.time() > stored["expires"]:
        del _otp_store[phone]
        return jsonify({"error": "OTP expired. Request a new one."}), 401
    if stored["otp"] != otp_submitted:
        return jsonify({"error": "Incorrect OTP"}), 401

    # OTP valid — clear it (single-use)
    del _otp_store[phone]

    # Upsert app_users
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO app_users (phone) VALUES (?)", (phone,)
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM app_users WHERE phone = ?", (phone,)
    ).fetchone()

    # Issue session token
    token = str(uuid.uuid4())
    _sessions[token] = {
        "app_user_id": row["id"],
        "phone": phone,
        "role": row["role"],
    }

    log.info("App user %s (%s) authenticated", row["id"], phone)
    return jsonify({
        "token": token,
        "user": {
            "id":    row["id"],
            "phone": phone,
            "role":  row["role"],
        }
    }), 200


# ─────────────────────────────────────────────────────────────
#  3.  GET /api/browse
#
#  Populates the mobile app's horizontal pill filters and
#  property card feed.  Mirrors bot's get_active_houses() logic.
#
#  Query params (all optional):
#    category     = Residential | Commercial          (pill filter)
#    neighborhood = Bole | CMC | Megenagna | Ayat … (sub-pill filter)
#    suitable_for = Cafe/Restaurant | Boutique/Cosmetics | …
#
#  Response:
#    200 {
#      "neighborhoods": ["Bole","CMC","Megenagna",...],   ← drives pills
#      "listings": [ { house fields … } , … ]
#    }
# ─────────────────────────────────────────────────────────────

@app.route("/api/browse", methods=["GET"])
def browse():
    category     = request.args.get("category")        # Residential | Commercial | None
    neighborhood = request.args.get("neighborhood")    # Bole | CMC | None
    search       = request.args.get("search", "").strip().lower()  # free text search
    suitable_for = request.args.get("suitable_for")    # comma-separated business types

    today = datetime.now().strftime("%Y-%m-%d")
    db    = get_db()

    # ── 1. Fetch available neighborhood pills (dynamic, from live DB) ──
    nb_query = """
        SELECT DISTINCT COALESCE(neighborhood, area) AS nb
        FROM houses
        WHERE status = 'active'
          AND (expiry_date IS NULL OR expiry_date >= ?)
    """
    nb_params: list = [today]
    if category:
        nb_query += " AND category = ?"
        nb_params.append(category)
    nb_rows = db.execute(nb_query, nb_params).fetchall()
    neighborhoods = [r["nb"] for r in nb_rows if r["nb"]]

    # ── 2. Build main listings query ──
    q = """
        SELECT id, area, neighborhood, rent_birr, description,
               photo_file_id, category, shop_dimensions,
               suitable_for, expiry_date, status
        FROM houses
        WHERE status = 'active'
          AND (expiry_date IS NULL OR expiry_date >= ?)
    """
    params: list = [today]

    if category:
        q += " AND category = ?"
        params.append(category)

    if neighborhood and neighborhood.lower() not in ("everywhere", "all"):
        q += " AND (neighborhood = ? OR area = ?)"
        params += [neighborhood, neighborhood]

    if search:
        q += " AND (LOWER(neighborhood) LIKE ? OR LOWER(area) LIKE ? OR LOWER(description) LIKE ? OR CAST(rent_birr AS TEXT) LIKE ?)"
        s = f"%{search}%"
        params += [s, s, s, s]

    q += " ORDER BY rowid DESC LIMIT 100"
    rows = db.execute(q, params).fetchall()

    # ── 3. Optional business-type sub-filter for Commercial listings ──
    if suitable_for and category == "Commercial":
        requested_types = [t.strip() for t in suitable_for.split(",") if t.strip()]

        def _matches(row) -> bool:
            if "Other/Any Business" in requested_types:
                return True
            prop_types = [t.strip() for t in (row["suitable_for"] or "").split(",")]
            if "Other/Any Business" in prop_types:
                return True
            return any(bt in prop_types for bt in requested_types)

        rows = [r for r in rows if _matches(r)]

    # ── 4. Serialize ──
    def _house_dict(h) -> dict:
        days_left = None
        if h["expiry_date"]:
            delta = datetime.strptime(h["expiry_date"], "%Y-%m-%d") - datetime.now()
            days_left = max(0, delta.days)
        return {
            "id":               h["id"],
            "area":             h["area"],
            "neighborhood":     h["neighborhood"] or h["area"],
            "rent_birr":        h["rent_birr"],
            "description":      h["description"],
            "has_photo":        bool(h["photo_file_id"]),
            "category":         h["category"] or "Residential",
            "shop_dimensions":  h["shop_dimensions"],
            "suitable_for":     h["suitable_for"],
            "days_left":        days_left,
        }

    return jsonify({
        "neighborhoods": neighborhoods,
        "listings":      [_house_dict(r) for r in rows],
    }), 200


# ─────────────────────────────────────────────────────────────
#  4a.  POST /api/pay/chapa/initiate
#
#  Step 1 of Chapa 2-step flow.
#  Calls Chapa's initialize endpoint and returns the checkout URL
#  for the app to open in a WebView.
#
#  Request body (JSON, requires auth):
#    {
#      "payment_type": "unlock" | "listing",
#      "house_id": "demo-001"          (required for unlock)
#    }
#
#  Response:
#    200 { "checkout_url": "https://checkout.chapa.co/…", "tx_ref": "…" }
# ─────────────────────────────────────────────────────────────

@app.route("/api/pay/chapa/initiate", methods=["POST"])
@_require_auth
def pay_chapa_initiate():
    data         = request.get_json(silent=True) or {}
    payment_type = data.get("payment_type")    # 'unlock' | 'listing'
    house_id     = data.get("house_id")
    user         = request.current_user

    if payment_type not in ("unlock", "listing"):
        return jsonify({"error": "payment_type must be 'unlock' or 'listing'"}), 400
    if payment_type == "unlock" and not house_id:
        return jsonify({"error": "house_id required for unlock payment"}), 400

    amount  = UNLOCK_FEE_BIRR  if payment_type == "unlock" else LISTING_FEE_BIRR
    title   = "BetLink Unlock Contact" if payment_type == "unlock" else "BetLink Post Listing"
    tx_ref  = f"app_{payment_type}_{user['app_user_id']}_{int(time.time())}"

    headers = {
        "Authorization": f"Bearer {CHAPA_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "amount":   str(amount),
        "currency": "ETB",
        "email":    f"app_user_{user['app_user_id']}@betlink.et",
        "first_name": "BetLink",
        "last_name":  "AppUser",
        "tx_ref":     tx_ref,
        "return_url": "https://betlink.et/app/payment-success",   # deep-link back to app
        "customization": {"title": title, "description": "BetLink Rental Platform"},
    }
    try:
        resp = requests.post(CHAPA_INIT_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200 and resp.json().get("status") == "success":
            checkout_url = resp.json()["data"]["checkout_url"]
            log.info("Chapa init OK [tx_ref=%s]", tx_ref)
            return jsonify({"checkout_url": checkout_url, "tx_ref": tx_ref}), 200
        log.warning("Chapa init failed [%s]: %s", tx_ref, resp.text[:200])
        return jsonify({"error": "Chapa initialization failed", "detail": resp.text[:200]}), 502
    except Exception as exc:
        log.error("Chapa init exception: %s", exc)
        return jsonify({"error": "Network error contacting Chapa"}), 502


# ─────────────────────────────────────────────────────────────
#  4b.  POST /api/pay/verify
#
#  Step 2 of Chapa 2-step flow  AND  manual screenshot submission.
#  This is the single endpoint the app calls after any payment.
#
#  Request body (JSON, requires auth):
#    Chapa path:
#      {
#        "method":       "chapa",
#        "tx_ref":       "app_unlock_3_1720000000",
#        "payment_type": "unlock" | "listing",
#        "house_id":     "demo-001"             (unlock only)
#      }
#
#    Manual path:
#      {
#        "method":       "manual",
#        "tx_ref":       "app_unlock_3_1720000000",   (generated client-side)
#        "payment_type": "unlock" | "listing",
#        "house_id":     "demo-001",                  (unlock only)
#        "screenshot":   "<base64-encoded PNG/JPG>"
#      }
#
#  Response (Chapa):
#    200 { "status": "approved",  "message": "Contact unlocked" }   ← instant
#    402 { "status": "pending",   "message": "Payment not confirmed by Chapa yet" }
#
#  Response (Manual):
#    202 { "status": "pending",   "message": "Submitted for admin review" }
# ─────────────────────────────────────────────────────────────

@app.route("/api/pay/verify", methods=["POST"])
@_require_auth
def pay_verify():
    data         = request.get_json(silent=True) or {}
    method       = data.get("method")         # 'chapa' | 'manual'
    tx_ref       = data.get("tx_ref", "").strip()
    payment_type = data.get("payment_type")   # 'unlock' | 'listing'
    house_id     = data.get("house_id")
    screenshot   = data.get("screenshot")     # base64, manual path only
    user         = request.current_user

    if method not in ("chapa", "manual"):
        return jsonify({"error": "method must be 'chapa' or 'manual'"}), 400
    if payment_type not in ("unlock", "listing"):
        return jsonify({"error": "payment_type must be 'unlock' or 'listing'"}), 400
    if not tx_ref:
        return jsonify({"error": "tx_ref is required"}), 400

    expected_amount = UNLOCK_FEE_BIRR if payment_type == "unlock" else LISTING_FEE_BIRR
    db = get_db()

    # ──────────────────────────────────────────────────────────
    #  PATH A: Chapa — call their verify endpoint
    # ──────────────────────────────────────────────────────────
    if method == "chapa":
        headers = {"Authorization": f"Bearer {CHAPA_SECRET_KEY}"}
        try:
            resp = requests.get(f"{CHAPA_VERIFY_URL}/{tx_ref}", headers=headers, timeout=10)
        except Exception as exc:
            log.error("Chapa verify network error: %s", exc)
            return jsonify({"error": "Network error contacting Chapa"}), 502

        if resp.status_code != 200:
            return jsonify({"status": "pending", "message": "Payment not confirmed by Chapa yet"}), 402

        resp_data = resp.json()
        if resp_data.get("status") != "success":
            return jsonify({"status": "pending", "message": "Payment not confirmed by Chapa yet"}), 402

        tx_data     = resp_data.get("data", {})
        paid_amount = int(float(tx_data.get("amount", 0)))

        # Guard: transaction-level status
        if tx_data.get("status") != "success":
            return jsonify({"status": "pending", "message": "Chapa transaction not in success state"}), 402

        # Guard: amount must match exactly
        if paid_amount != expected_amount:
            log.warning("Chapa amount mismatch [tx=%s]: expected %s got %s", tx_ref, expected_amount, paid_amount)
            return jsonify({"status": "error", "message": f"Amount mismatch: expected {expected_amount} ETB"}), 402

        # ── Payment confirmed — execute the business action ──
        if payment_type == "unlock" and house_id:
            db.execute(
                "INSERT OR IGNORE INTO unlocked (renter_id, house_id) VALUES (?,?)",
                (user["app_user_id"], house_id),
            )
            db.commit()
            log.info("App user %s unlocked house %s via Chapa", user["app_user_id"], house_id)
            return jsonify({"status": "approved", "message": "Contact unlocked successfully"}), 200

        if payment_type == "listing":
            # Mark the newest pending listing by this app user as active
            expiry = (datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)).strftime("%Y-%m-%d")
            db.execute(
                """
                UPDATE houses SET status = 'active', expiry_date = ?
                WHERE landlord_id = ? AND status = 'pending'
                ORDER BY rowid DESC LIMIT 1
                """,
                (expiry, user["app_user_id"]),
            )
            db.commit()
            log.info("App user %s listing activated via Chapa", user["app_user_id"])
            return jsonify({"status": "approved", "message": "Listing published for 30 days"}), 200

    # ──────────────────────────────────────────────────────────
    #  PATH B: Manual screenshot — store for admin review
    #  Mirrors the bot's rn_manual_upload flow.
    # ──────────────────────────────────────────────────────────
    if method == "manual":
        if not screenshot:
            return jsonify({"error": "screenshot (base64) is required for manual payment"}), 400

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO manual_payments
                  (app_user_id, house_id, tx_ref, payment_type, amount_birr, screenshot, status)
                VALUES (?,?,?,?,?,?,'pending')
                """,
                (user["app_user_id"], house_id, tx_ref, payment_type, expected_amount, screenshot),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({"status": "pending", "message": "Already submitted"}), 202

        log.info(
            "Manual payment submitted: user=%s type=%s house=%s tx=%s",
            user["app_user_id"], payment_type, house_id, tx_ref,
        )

        # Notify admin on Telegram (best-effort; requires bot token in env)
        _notify_admin_manual_payment(user, payment_type, house_id, tx_ref, expected_amount)

        return jsonify({
            "status":  "pending",
            "message": "Receipt submitted. Admin will approve within a few hours.",
            "tx_ref":  tx_ref,
            "cbe":     CBE_ACCOUNT,
            "telebirr": TELEBIRR_ACCOUNT,
        }), 202


def _notify_admin_manual_payment(user: dict, payment_type: str, house_id: str | None,
                                  tx_ref: str, amount: int) -> None:
    """
    Fire-and-forget Telegram message to the admin about a new manual
    payment submission so they can approve it from the bot interface.
    """
    bot_token = os.environ.get("BETLINK_BOT_TOKEN", "")
    if not bot_token:
        return
    text = (
        f"📱 <b>New App Manual Payment</b>\n"
        f"Type: <b>{payment_type}</b>\n"
        f"User: app_user_{user['app_user_id']} | {user['phone']}\n"
        f"House: {house_id or 'N/A'}\n"
        f"Amount: <b>{amount} ETB</b>\n"
        f"Tx ref: <code>{tx_ref}</code>\n\n"
        f"Review in admin queue."
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": ADMIN_TELEGRAM_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass   # Non-critical notification


# ─────────────────────────────────────────────────────────────
#  5.  GET /api/user/library
#
#  Returns the list of property IDs this app user has already
#  unlocked — the app's "Unlocked" tab screen.
#  Also includes full property card data for rendering.
#
#  Requires: Authorization: Bearer <token>
#
#  Response:
#    200 {
#      "unlocked_ids": ["demo-001", "demo-004", …],
#      "listings": [ { full property data + phone revealed } ]
#    }
# ─────────────────────────────────────────────────────────────

@app.route("/api/user/library", methods=["GET"])
@_require_auth
def user_library():
    user = request.current_user
    db   = get_db()

    rows = db.execute(
        """
        SELECT h.id, h.area, h.neighborhood, h.rent_birr, h.description,
               h.phone, h.category, h.shop_dimensions, h.suitable_for,
               h.expiry_date, h.status
        FROM   houses h
        INNER  JOIN unlocked u ON h.id = u.house_id
        WHERE  u.renter_id = ?
        ORDER  BY h.rowid DESC
        """,
        (user["app_user_id"],),
    ).fetchall()

    def _unlocked_dict(h) -> dict:
        days_left = None
        if h["expiry_date"]:
            delta = datetime.strptime(h["expiry_date"], "%Y-%m-%d") - datetime.now()
            days_left = max(0, delta.days)
        return {
            "id":              h["id"],
            "area":            h["area"],
            "neighborhood":    h["neighborhood"] or h["area"],
            "rent_birr":       h["rent_birr"],
            "description":     h["description"],
            "phone":           h["phone"],          # revealed because already paid
            "category":        h["category"] or "Residential",
            "shop_dimensions": h["shop_dimensions"],
            "suitable_for":    h["suitable_for"],
            "days_left":       days_left,
            "listing_active":  h["status"] == "active",
        }

    unlocked_ids = [r["id"] for r in rows]
    return jsonify({
        "unlocked_ids": unlocked_ids,
        "listings":     [_unlocked_dict(r) for r in rows],
    }), 200


# ─────────────────────────────────────────────────────────────
#  BONUS — GET /api/user/profile
#  Profile screen: role, stats, account info
# ─────────────────────────────────────────────────────────────

@app.route("/api/user/profile", methods=["GET"])
@_require_auth
def user_profile():
    user = request.current_user
    db   = get_db()

    unlocked_count = db.execute(
        "SELECT COUNT(*) AS c FROM unlocked WHERE renter_id = ?",
        (user["app_user_id"],),
    ).fetchone()["c"]

    app_row = db.execute(
        "SELECT * FROM app_users WHERE id = ?", (user["app_user_id"],)
    ).fetchone()

    return jsonify({
        "id":             user["app_user_id"],
        "phone":          user["phone"],
        "role":           app_row["role"] if app_row else user["role"],
        "unlocked_count": unlocked_count,
        "member_since":   app_row["created_at"] if app_row else None,
    }), 200


# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
#  GET /api/admin/queue
#  Returns all pending manual payments for the admin review screen.
# ─────────────────────────────────────────────────────────────

@app.route("/api/admin/queue", methods=["GET"])
def admin_queue():
    if request.headers.get("X-Admin-Key") != APP_SECRET:
        return jsonify({"error": "Forbidden"}), 403

    db = get_db()
    rows = db.execute(
        """SELECT id, app_user_id, house_id, tx_ref, payment_type,
                  amount_birr, screenshot, submitted_at
           FROM manual_payments
           WHERE status = 'pending'
           ORDER BY submitted_at DESC"""
    ).fetchall()

    payments = []
    for r in rows:
        payments.append({
            "id":             r["id"],
            "app_user_id":    r["app_user_id"],
            "house_id":       r["house_id"],
            "tx_ref":         r["tx_ref"],
            "payment_type":   r["payment_type"],
            "amount_birr":    r["amount_birr"],
            "has_screenshot": bool(r["screenshot"]),
            "submitted_at":   r["submitted_at"],
        })

    return jsonify({"pending": payments, "count": len(payments)}), 200


#  BONUS — POST /api/admin/manual-payment/action
#  Admin approves or rejects a manual payment from the app queue.
#  Mirrors the bot's admin approval flow.
# ─────────────────────────────────────────────────────────────

@app.route("/api/admin/manual-payment/action", methods=["POST"])
def admin_payment_action():
    # Lightweight admin key check (replace with proper admin auth in prod)
    if request.headers.get("X-Admin-Key") != APP_SECRET:
        return jsonify({"error": "Forbidden"}), 403

    data   = request.get_json(silent=True) or {}
    tx_ref = data.get("tx_ref")
    action = data.get("action")   # 'approve' | 'reject'

    if not tx_ref or action not in ("approve", "reject"):
        return jsonify({"error": "tx_ref and action ('approve'|'reject') required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT * FROM manual_payments WHERE tx_ref = ?", (tx_ref,)
    ).fetchone()

    if not row:
        return jsonify({"error": "Payment record not found"}), 404
    if row["status"] != "pending":
        return jsonify({"error": f"Payment already {row['status']}"}), 409

    db.execute(
        "UPDATE manual_payments SET status = ? WHERE tx_ref = ?",
        (action + "d", tx_ref),   # 'approved' or 'rejected'
    )

    if action == "approve":
        if row["payment_type"] == "unlock" and row["house_id"]:
            db.execute(
                "INSERT OR IGNORE INTO unlocked (renter_id, house_id) VALUES (?,?)",
                (row["app_user_id"], row["house_id"]),
            )
            log.info("Admin approved manual unlock: user=%s house=%s", row["app_user_id"], row["house_id"])
        elif row["payment_type"] == "listing":
            expiry = (datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)).strftime("%Y-%m-%d")
            db.execute(
                """
                UPDATE houses SET status='active', expiry_date=?
                WHERE landlord_id=? AND status='pending'
                ORDER BY rowid DESC LIMIT 1
                """,
                (expiry, row["app_user_id"]),
            )
            log.info("Admin approved manual listing: user=%s", row["app_user_id"])

# ── Notify the user about the payment decision ──
    if action == "approve":
        if row["payment_type"] == "unlock":
            notif_msg = "✅ Your payment was approved! The property contact has been unlocked."
        else:
            notif_msg = "✅ Your listing has been approved and is now live for 30 days."
    else:
        if row["payment_type"] == "unlock":
            notif_msg = "❌ Your unlock payment was rejected. Please resubmit with a clear receipt."
        else:
            notif_msg = "❌ Your listing payment was rejected. Please resubmit with a clear receipt."

    db.execute(
        "INSERT INTO notifications (app_user_id, message) VALUES (?, ?)",
        (row["app_user_id"], notif_msg),
    )

    db.commit()
    return jsonify({"status": action + "d", "tx_ref": tx_ref}), 200



# ─────────────────────────────────────────────────────────────
#  6.  POST /api/listings/draft
#
#  Saves a new property listing as 'pending' status.
#  Called by the landlord form BEFORE the payment step.
#  Once admin approves the manual payment (or Chapa confirms),
#  the listing status is flipped to 'active'.
#
#  Request body (JSON, requires auth):
#    {
#      "category":        "Residential" | "Commercial",
#      "neighborhood":    "Bole",
#      "rent_birr":       15000,
#      "phone":           "+251911234567",
#      "description":     "3-bedroom apartment…",
#      "shop_dimensions": "6x4",          (Commercial only, or null)
#      "suitable_for":    "Cafe, Salon"   (Commercial only, or null)
#    }
#
#  Response:
#    201 { "message": "Listing draft saved", "house_id": 42 }
# ─────────────────────────────────────────────────────────────

@app.route("/api/listings/draft", methods=["POST"])
@_require_auth
def listings_draft():
    data     = request.get_json(silent=True) or {}
    user     = request.current_user

    category        = data.get("category", "").strip()
    neighborhood    = data.get("neighborhood", "").strip()
    rent_birr       = data.get("rent_birr")
    phone           = data.get("phone", "").strip()
    description     = data.get("description", "").strip()
    shop_dimensions = data.get("shop_dimensions")
    suitable_for    = data.get("suitable_for")
    photos_list     = data.get("photos", [])   # list of base64 strings (max 5)

    # Basic validation
    if category not in ("Residential", "Commercial"):
        return jsonify({"error": "category must be 'Residential' or 'Commercial'"}), 400
    if not neighborhood:
        return jsonify({"error": "neighborhood is required"}), 400
    if not rent_birr or not isinstance(rent_birr, (int, float)) or rent_birr < 100:
        return jsonify({"error": "rent_birr must be a number >= 100"}), 400
    if not phone:
        return jsonify({"error": "phone is required"}), 400
    if not description:
        return jsonify({"error": "description is required"}), 400

    # Cap photos at 5 and store as JSON string
    import json as _json
    photos_json = _json.dumps(photos_list[:5]) if photos_list else None

    db = get_db()

    # Add photos column if it doesn't exist yet (safe migration)
    try:
        db.execute("ALTER TABLE houses ADD COLUMN photos TEXT")
        db.commit()
    except Exception:
        pass  # Column already exists — that's fine

    # Delete any previous pending draft from this user so the queue stays clean
    db.execute(
        "DELETE FROM houses WHERE landlord_id = ? AND status = 'pending'",
        (user["app_user_id"],)
    )

    # Insert the new draft listing
    cursor = db.execute(
        """
        INSERT INTO houses
          (area, neighborhood, rent_birr, phone, description,
           shop_dimensions, suitable_for, category, landlord_id, status, photos)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            neighborhood,       # area (reused field)
            neighborhood,
            int(rent_birr),
            phone,
            description,
            shop_dimensions,
            suitable_for,
            category,
            user["app_user_id"],
            photos_json,
        ),
    )
    db.commit()

    house_id = cursor.lastrowid
    photo_count = len(photos_list[:5]) if photos_list else 0
    log.info(
        "Listing draft created: user=%s house_id=%s category=%s neighborhood=%s rent=%s photos=%s",
        user["app_user_id"], house_id, category, neighborhood, rent_birr, photo_count,
    )

    return jsonify({"message": "Listing draft saved", "house_id": house_id}), 201
# ─────────────────────────────────────────────────────────────
#  GET /api/user/notifications
#  Returns all notifications for the logged-in user.
# ─────────────────────────────────────────────────────────────

@app.route("/api/user/notifications", methods=["GET"])
@_require_auth
def user_notifications():
    user = request.current_user
    db   = get_db()
    rows = db.execute(
        """SELECT id, message, is_read, created_at
           FROM notifications
           WHERE app_user_id = ?
           ORDER BY created_at DESC
           LIMIT 50""",
        (user["app_user_id"],)
    ).fetchall()

    notifications = [
        {
            "id":         r["id"],
            "message":    r["message"],
            "is_read":    bool(r["is_read"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    unread_count = sum(1 for n in notifications if not n["is_read"])
    return jsonify({"notifications": notifications, "unread_count": unread_count}), 200


# ─────────────────────────────────────────────────────────────
#  POST /api/user/notifications/read-all
#  Marks all of the logged-in user's notifications as read.
# ─────────────────────────────────────────────────────────────

@app.route("/api/user/notifications/read-all", methods=["POST"])
@_require_auth
def user_notifications_read_all():
    user = request.current_user
    db   = get_db()
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE app_user_id = ?",
        (user["app_user_id"],)
    )
    db.commit()
    return jsonify({"status": "ok"}), 200


@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    if request.headers.get("X-Admin-Key") != APP_SECRET:
        return jsonify({"error": "Unauthorized"}), 403
    db = get_db()
    rows = db.execute(
        "SELECT id, phone, role, created_at FROM app_users ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]}), 200

# ─────────────────────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    db = get_db()
    listing_count = db.execute(
        "SELECT COUNT(*) AS c FROM houses WHERE status='active'"
    ).fetchone()["c"]
    return jsonify({
        "status":          "ok",
        "db":              DB_PATH,
        "active_listings": listing_count,
        "timestamp":       datetime.utcnow().isoformat() + "Z",
    }), 200


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

def _seed_test_session() -> None:
    """
    Matches the frontend's OTP test bypass (otp === '123456' -> token 'test-token').
    Without this, /api/pay/verify and other authenticated routes reject
    'test-token' with "Invalid or expired session token".
    Safe to leave in for now; remove this function (and its call below)
    before real launch.
    """
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode = WAL;")
    db.execute(
        "INSERT OR IGNORE INTO app_users (phone, role) VALUES (?, ?)",
        ("0900000000", "renter"),
    )
    db.commit()
    row = db.execute(
        "SELECT id, phone, role FROM app_users WHERE phone = ?", ("0900000000",)
    ).fetchone()
    db.close()

    _sessions["test-token"] = {
        "app_user_id": row[0],
        "phone":       row[1],
        "role":        row[2],
    }
    log.info("Seeded OTP test-bypass session (test-token -> app_user_id=%s)", row[0])


_ensure_app_tables()   # Safe: only adds new tables, never touches bot tables
_seed_test_session()


if __name__ == "__main__":
    log.info("BetLink API starting on port 5000 …")
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("API_PORT", 5000)),
        debug=False,          # Set True only in development
    )
