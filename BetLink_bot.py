# ============================================================
#  BetLink Bot — Real Estate Matching Bot for Ethiopia
#  Library : pyTelegramBotAPI (telebot)
#  Storage : SQLite (standalone, no external DB needed)
#  Author  : Production-Grade Implementation
#  Version : 2.0 — Commercial Category + Business Type Matching
# ============================================================

import os
import re
import logging
import sqlite3
import uuid
import time
import requests
import telebot
from datetime import datetime, timedelta
from telebot import types

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BETLINK_BOT_TOKEN", "8679149165:AAHB_ph2WSS8NfHIB4ozWXmSxwy5vY8Dnqo")

# !! Replace with your real Chapa test secret key !!
CHAPA_SECRET_KEY = os.environ.get("CHAPA_SECRET_KEY", "CHASECK_TEST-MysUHnycnxWFrB4s3XiAVLkdTDIpOdCf")

# !! Replace with your real Telegram user ID — get it from @userinfobot !!
ADMIN_ID: int = 1602487678

LISTING_FEE_BIRR: int = 200        # Landlord listing fee
UNLOCK_FEE_BIRR:  int = 100        # Renter contact-unlock fee

# Official manual transfer accounts
CBE_ACCOUNT: str = "1000385968397 (BetLink Platform)"
TELEBIRR_ACCOUNT: str = "0920323890 (BetLink Platform)"

SUPPORT_USERNAME: str = "@BetLinkSupport"

LISTING_ACTIVE_DAYS: int = 30      # Days a listing stays active after admin approval

FALLBACK_PHOTO_URL: str = (
    "https://via.placeholder.com/800x500.png?text=No+Photo+Available"
)

# Ethiopian phone number regex:
# Accepts: 09xxxxxxxx, 07xxxxxxxx, +2519xxxxxxxx, +2517xxxxxxxx
ETHIOPIAN_PHONE_RE = re.compile(
    r"^(?:(?:09|07)\d{8}|(?:\+2519|\+2517)\d{8})$"
)

# Dimension format: two positive integers separated by 'x' (case-insensitive)
# Examples: 4x3, 6x4, 10x5
DIMENSION_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")

# Ethiopian commercial business profiles shown as toggles
BUSINESS_TYPES = [
    "Cafe/Restaurant",
    "Boutique/Cosmetics",
    "Electronics Shop",
    "Pool Game Zone",
    "Beauty Salon",
    "Other/Any Business",
]

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BetLinkBot")

# ─────────────────────────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────────────────────────
DB_PATH = "betlink.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id   INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                role          TEXT DEFAULT 'renter'
            );

            CREATE TABLE IF NOT EXISTS houses (
                id              TEXT    PRIMARY KEY,
                landlord_id     INTEGER NOT NULL,
                area            TEXT    NOT NULL,
                rent_birr       INTEGER NOT NULL,
                description     TEXT    NOT NULL,
                photo_file_id   TEXT,
                phone           TEXT    NOT NULL,
                status          TEXT    DEFAULT 'pending',
                expiry_date     TEXT    DEFAULT NULL,
                category        TEXT    DEFAULT 'Residential',
                shop_dimensions TEXT    DEFAULT NULL,
                suitable_for    TEXT    DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS unlocked (
                renter_id   INTEGER NOT NULL,
                house_id    TEXT    NOT NULL,
                PRIMARY KEY (renter_id, house_id)
            );
            """
        )

                # ── Non-destructive migrations for existing deployments ──
        _safe_add_column(conn, "houses", "expiry_date",     "TEXT DEFAULT NULL")
        _safe_add_column(conn, "houses", "category",        "TEXT DEFAULT 'Residential'")
        _safe_add_column(conn, "houses", "shop_dimensions", "TEXT DEFAULT NULL")
        _safe_add_column(conn, "houses", "suitable_for",    "TEXT DEFAULT NULL")
        _safe_add_column(conn, "houses", "neighborhood",    "TEXT DEFAULT NULL")

        # ── Demo residential listings ──
        demo_expiry = (datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)).strftime("%Y-%m-%d")
        demo_houses = [
            (
                "demo-001", 0, "Bole", 8500,
                "Modern 2-bedroom apartment in the heart of Bole.\n"
                "- Floor: 4th\n- Furnished: Yes\n- Wi-Fi included\n"
                "- Walking distance to Edna Mall",
                None, "+251911111111", demo_expiry, "Residential", None, None, "Bole",
            ),
            (
                "demo-002", 0, "Megenagna", 6000,
                "Cozy 1-bedroom near Megenagna roundabout.\n"
                "- Floor: 2nd\n- Furnished: Partially\n"
                "- Easy access to public transport",
                None, "+251922222222", demo_expiry, "Residential", None, None, "Megenagna",
            ),
            (
                "demo-003", 0, "Kazanchis", 12000,
                "Spacious 3-bedroom family apartment in Kazanchis.\n"
                "- Floor: 1st\n- Compound parking\n"
                "- 24-hour security guard",
                None, "+251933333333", demo_expiry, "Residential", None, None, "Kazanchis",
            ),
            # ── Demo commercial listing ──
            (
                "demo-004", 0, "Bole", 15000,
                "Prime ground-floor shop space on Bole Road.\n"
                "- High foot traffic\n- Display windows facing the street\n"
                "- Water and electricity included",
                None, "+251944444444", demo_expiry, "Commercial", "6x4",
                "Cafe/Restaurant, Boutique/Cosmetics", "Bole",
            ),
        ]
        for house in demo_houses:
            conn.execute(
                """
                INSERT OR IGNORE INTO houses
                  (id, landlord_id, area, rent_birr, description,
                   photo_file_id, phone, status, expiry_date,
                   category, shop_dimensions, suitable_for, neighborhood)
                VALUES (?,?,?,?,?,?,?,'active',?,?,?, ?, ?)
                """,
                house,
            )

        conn.commit()
    log.info("Database initialised at '%s'.", DB_PATH)


def _safe_add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column to a table if it does not already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        log.info("Migrated: added column '%s' to '%s'.", column, table)
    except sqlite3.OperationalError:
        pass  # Column already exists


# ─────────────────────────────────────────────────────────────
#  BOT INSTANCE
# ─────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ─────────────────────────────────────────────────────────────
#  IN-MEMORY STATE
# ─────────────────────────────────────────────────────────────
user_state: dict[int, dict] = {}


def renter_neighborhood_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("[ Bole ]",       callback_data="nb_Bole"),
        types.InlineKeyboardButton("[ CMC ]",        callback_data="nb_CMC"),
    )
    kb.row(
        types.InlineKeyboardButton("[ Megenagna ]",  callback_data="nb_Megenagna"),
        types.InlineKeyboardButton("[ Ayat ]",       callback_data="nb_Ayat"),
    )
    kb.row(
        types.InlineKeyboardButton("[ 🌍 Everywhere / Show All ]", callback_data="nb_Everywhere"),
    )
    return kb


# ─────────────────────────────────────────────────────────────
#  CHAPA — INITIALIZE PAYMENT
# ─────────────────────────────────────────────────────────────
CHAPA_INIT_URL   = "https://api.chapa.co/v1/transaction/initialize"
CHAPA_VERIFY_URL = "https://api.chapa.co/v1/transaction/verify"


def get_real_chapa_link(tx_ref: str, amount: int, title: str) -> str:
    """
    Call Chapa's initialize endpoint and return the checkout URL.
    Falls back to 'https://chapa.co' if the API call fails.
    """
    headers = {
        "Authorization": f"Bearer {CHAPA_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "amount": str(amount),
        "currency": "ETB",
        "email": "customer@betlink.com",
        "first_name": "BetLink",
        "last_name": "Customer",
        "tx_ref": tx_ref,
        "return_url": "https://t.me/BetLinkBot",
        "customization": {
            "title": str(title),
            "description": "BetLink Rental Platform Service",
        },
    }
    try:
        response = requests.post(
            CHAPA_INIT_URL,
            json=payload,
            headers=headers,
            timeout=10,
        )
        log.info(
            "Chapa init [tx_ref=%s]: HTTP %s — %s",
            tx_ref, response.status_code, response.text[:200],
        )
        if response.status_code == 200:
            res_data = response.json()
            if res_data.get("status") == "success":
                checkout_url = res_data["data"]["checkout_url"]
                log.info("Chapa checkout URL generated: %s", checkout_url)
                return checkout_url
            else:
                log.warning(
                    "Chapa init non-success [tx_ref=%s]: %s", tx_ref, res_data
                )
        else:
            log.warning(
                "Chapa init non-200 [tx_ref=%s]: HTTP %s", tx_ref, response.status_code
            )
    except requests.exceptions.Timeout:
        log.error("Chapa init timed out [tx_ref=%s]", tx_ref)
    except requests.exceptions.ConnectionError:
        log.error("Chapa init connection error [tx_ref=%s]", tx_ref)
    except Exception as exc:
        log.error("Chapa init unexpected error [tx_ref=%s]: %s", tx_ref, exc)

    return "https://chapa.co"


# ─────────────────────────────────────────────────────────────
#  CHAPA — VERIFY PAYMENT
# ─────────────────────────────────────────────────────────────

def verify_chapa_payment(tx_ref: str, expected_amount: int) -> bool:
    """
    Call Chapa's verify endpoint and confirm:
      1. The top-level status is 'success'
      2. The transaction-level status is also 'success'
      3. The amount paid matches expected_amount exactly (in ETB)

    Returns True only when ALL three conditions pass.
    Returns False on any mismatch, API error, or network failure.
    """
    headers = {
        "Authorization": f"Bearer {CHAPA_SECRET_KEY}",
    }
    try:
        response = requests.get(
            f"{CHAPA_VERIFY_URL}/{tx_ref}",
            headers=headers,
            timeout=10,
        )
        log.info(
            "Chapa verify [tx_ref=%s]: HTTP %s — %s",
            tx_ref, response.status_code, response.text[:300],
        )

        if response.status_code != 200:
            log.warning(
                "Chapa verify non-200 [tx_ref=%s]: %s", tx_ref, response.status_code
            )
            return False

        data = response.json()

        # Guard 1: top-level status must be 'success'
        if data.get("status") != "success":
            log.warning(
                "Chapa verify top-level status not success [tx_ref=%s]: %s",
                tx_ref, data,
            )
            return False

        tx_data = data.get("data", {})

        # Guard 2: transaction-level status must also be 'success'
        if tx_data.get("status") != "success":
            log.warning(
                "Chapa verify tx-level status not success [tx_ref=%s]: %s",
                tx_ref, tx_data.get("status"),
            )
            return False

        # Guard 3: amount must match exactly
        # Chapa returns amount as a string e.g. "200.00" — convert to int
        paid_amount = int(float(tx_data.get("amount", 0)))
        if paid_amount != expected_amount:
            log.warning(
                "Chapa amount mismatch [tx_ref=%s]: expected %s, got %s",
                tx_ref, expected_amount, paid_amount,
            )
            return False

        log.info(
            "Chapa payment verified OK [tx_ref=%s, amount=%s Birr].",
            tx_ref, paid_amount,
        )
        return True

    except requests.exceptions.Timeout:
        log.error("Chapa verify timed out [tx_ref=%s]", tx_ref)
    except requests.exceptions.ConnectionError:
        log.error("Chapa verify connection error [tx_ref=%s]", tx_ref)
    except Exception as exc:
        log.error("Chapa verify unexpected error [tx_ref=%s]: %s", tx_ref, exc)

    return False


# ─────────────────────────────────────────────────────────────
#  DB HELPERS
# ─────────────────────────────────────────────────────────────

def upsert_user(user: types.User) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE
              SET username   = excluded.username,
                  first_name = excluded.first_name
            """,
            (user.id, user.username or "", user.first_name or ""),
        )
        conn.commit()


def get_active_houses(category: str | None = None, neighborhood: str | None = None) -> list[sqlite3.Row]:
    """Return all active, non-expired listings. Optionally filter by category and neighborhood."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        base = """
            SELECT * FROM houses
            WHERE status = 'active'
              AND (expiry_date IS NULL OR expiry_date >= ?)
        """
        params: list = [today]
        if category:
            base += " AND category = ?"
            params.append(category)
        if neighborhood and neighborhood != "Everywhere":
            base += " AND (neighborhood = ? OR area = ?)"
            params.append(neighborhood)
            params.append(neighborhood)
        base += " ORDER BY rowid DESC"
        return conn.execute(base, tuple(params)).fetchall()


def get_active_neighborhoods(category: str | None = None) -> list[str]:
    """Return distinct neighborhoods that currently have active listings, optionally by category."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        if category:
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(neighborhood, area) AS nb FROM houses
                WHERE status = 'active'
                  AND (expiry_date IS NULL OR expiry_date >= ?)
                  AND category = ?
                """,
                (today, category),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(neighborhood, area) AS nb FROM houses
                WHERE status = 'active'
                  AND (expiry_date IS NULL OR expiry_date >= ?)
                """,
                (today,),
            ).fetchall()
    return [r["nb"] for r in rows if r["nb"]]



def get_commercial_houses_by_business(business_types: list[str],
                                      neighborhood: str | None = None) -> list[sqlite3.Row]:
    """
    Return active commercial listings whose suitable_for field overlaps
    with any of the requested business_types.  'Other/Any Business' matches all.
    Optionally filter by neighbourhood (matches area column).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        query = (
            "SELECT * FROM houses "
            "WHERE status = 'active' "
            "AND (expiry_date IS NULL OR expiry_date >= ?) "
            "AND category = 'Commercial'"
        )
        params: list = [today]
        if neighborhood:
            query += " AND (neighborhood = ? OR area = ?)"
            params += [neighborhood, neighborhood]
        query += " ORDER BY rowid DESC"
        rows = conn.execute(query, tuple(params)).fetchall()

    if "Other/Any Business" in business_types:
        return rows  # Match everything

    matched = []
    for row in rows:
        prop_types_raw = row["suitable_for"] or ""
        prop_types = [t.strip() for t in prop_types_raw.split(",") if t.strip()]
        # If the property says "Other/Any Business" it matches all searches too
        if "Other/Any Business" in prop_types:
            matched.append(row)
            continue
        if any(bt in prop_types for bt in business_types):
            matched.append(row)
    return matched


def get_house_by_id(house_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM houses WHERE id = ?", (house_id,)
        ).fetchone()


def get_landlord_houses(landlord_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM houses
            WHERE landlord_id = ?
              AND status != 'inactive'
            ORDER BY rowid DESC
            """,
            (landlord_id,),
        ).fetchall()


def is_contact_unlocked(renter_id: int, house_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM unlocked WHERE renter_id=? AND house_id=?",
            (renter_id, house_id),
        ).fetchone()
        return row is not None


def unlock_contact(renter_id: int, house_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO unlocked (renter_id, house_id) VALUES (?,?)",
            (renter_id, house_id),
        )
        conn.commit()


def get_unlocked_houses_for_renter(renter_id: int) -> list[sqlite3.Row]:
    """Return full house rows for all contacts this renter has unlocked."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT h.* FROM houses h
            INNER JOIN unlocked u ON h.id = u.house_id
            WHERE u.renter_id = ?
            ORDER BY h.rowid DESC
            """,
            (renter_id,),
        ).fetchall()


def validate_ethiopian_phone(phone: str) -> bool:
    """Return True if phone matches a valid Ethiopian mobile format."""
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    return bool(ETHIOPIAN_PHONE_RE.match(cleaned))


# ─────────────────────────────────────────────────────────────
#  BUSINESS-TYPE MULTI-SELECT KEYBOARD HELPERS
# ─────────────────────────────────────────────────────────────

def _build_business_type_kb(selected: list[str]) -> types.InlineKeyboardMarkup:
    """
    Render the business-type toggle keyboard.
    Selected items show a ✅ prefix.
    callback_data format: "biz_toggle_{type_index}"
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for i, bt in enumerate(BUSINESS_TYPES):
        label = f"✅ {bt}" if bt in selected else bt
        buttons.append(
            types.InlineKeyboardButton(label, callback_data=f"biz_toggle_{i}")
        )
    # Add buttons in pairs
    for i in range(0, len(buttons), 2):
        row = buttons[i:i+2]
        kb.row(*row)
    kb.row(
        types.InlineKeyboardButton("[ ➕ Done Selecting ]", callback_data="biz_done")
    )
    return kb


# ─────────────────────────────────────────────────────────────
#  KEYBOARD BUILDERS
# ─────────────────────────────────────────────────────────────

def landlord_main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(
        types.KeyboardButton("[ 🏠 Post a New House ]"),
        types.KeyboardButton("[ 📋 My Active Listings ]"),
        types.KeyboardButton("[ 📞 Contact Support ]"),
    )
    return kb


def renter_main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(
        types.KeyboardButton("[ 🔍 Browse Listings ]"),
        types.KeyboardButton("[ 📋 My Unlocked Contacts ]"),
        types.KeyboardButton("[ 📞 Contact Support ]"),
    )
    return kb


def role_selection_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("I am a Landlord 🏠", callback_data="role_landlord"),
        types.InlineKeyboardButton("I am a Renter 🔍",   callback_data="role_renter"),
    )
    return kb


def renter_browse_markup(
    house_id: str,
    index: int,
    total: int,
) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=3)
    prev_btn = types.InlineKeyboardButton(
        f"◀ Prev ({index}/{total})", callback_data=f"prev_house_{index}"
    )
    next_btn = types.InlineKeyboardButton(
        f"Next ({index}/{total}) ▶", callback_data=f"next_house_{index}"
    )
    unlock_btn = types.InlineKeyboardButton(
        f"🔓 Unlock Contact ({UNLOCK_FEE_BIRR} Birr)",
        callback_data=f"unlock_id_{house_id}",
    )
    kb.row(prev_btn, next_btn)
    kb.row(unlock_btn)
    return kb


def admin_approval_markup(house_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{house_id}"),
        types.InlineKeyboardButton("❌ Reject",  callback_data=f"admin_reject_{house_id}"),
    )
    return kb


def renter_category_markup() -> types.InlineKeyboardMarkup:
    """Ask the renter which category they want to browse."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🏠 Residential Homes",   callback_data="browse_cat_Residential"),
        types.InlineKeyboardButton("🏢 Commercial Spaces",   callback_data="browse_cat_Commercial"),
        types.InlineKeyboardButton("🔎 All Listings",        callback_data="browse_cat_All"),
    )
    return kb


# ─────────────────────────────────────────────────────────────
#  SEND / EDIT a house card for renters
# ─────────────────────────────────────────────────────────────

def _build_house_caption(house: sqlite3.Row, index: int, total: int) -> str:
    """Build the photo-card caption, adapting for residential vs commercial."""
    area     = house["area"]
    nb       = house.get("neighborhood") if isinstance(house, dict) else (house["neighborhood"] if "neighborhood" in house.keys() else None)
    loc_line = f"<b>📍 Location:</b> {area}" + (f"  |  <b>Zone:</b> {nb}" if nb else "")
    rent     = house["rent_birr"]
    desc     = house["description"]
    expiry   = house["expiry_date"] or "N/A"
    category = house["category"] or "Residential"

    if category == "Commercial":
        dims_raw = house["shop_dimensions"] or ""
        m = DIMENSION_RE.match(dims_raw)
        dims_display = f"{m.group(1)}m x {m.group(2)}m" if m else dims_raw
        suitable = house["suitable_for"] or "Any Business"

        category_line  = f"🏢 <b>Category:</b> Commercial Space  |  📐 <b>Dimensions:</b> {dims_display}"
        business_line  = f"💼 <b>Best For:</b> {suitable}"
        caption = (
            f"<b>Property #{index} of {total}</b>\n"
            f"{category_line}\n"
            f"{business_line}\n"
            f"{loc_line}\n"
            f"<b>💰 Rent:</b> {rent:,} Birr / month\n"
            f"<b>📅 Listed until:</b> {expiry}\n\n"
            f"{desc}\n\n"
            f"<i>Unlock contact info for {UNLOCK_FEE_BIRR} Birr to get the "
            f"landlord's phone number.</i>"
        )
    else:
        caption = (
            f"<b>Property #{index} of {total}</b>\n"
            f"🏠 <b>Category:</b> Residential Home\n"
            f"{loc_line}\n"
            f"<b>💰 Rent:</b> {rent:,} Birr / month\n"
            f"<b>📅 Listed until:</b> {expiry}\n\n"
            f"{desc}\n\n"
            f"<i>Unlock contact info for {UNLOCK_FEE_BIRR} Birr to get the "
            f"landlord's phone number.</i>"
        )
    return caption



def send_house_card(
    chat_id: int,
    house: sqlite3.Row,
    index: int,
    total: int,
    message_id_to_edit: int | None = None,
) -> None:
    house_id  = house["id"]
    photo_fid = house["photo_file_id"]

    caption = _build_house_caption(house, index, total)
    markup  = renter_browse_markup(house_id, index, total)
    photo   = photo_fid if photo_fid else FALLBACK_PHOTO_URL

    if message_id_to_edit:
        try:
            bot.delete_message(chat_id, message_id_to_edit)
        except Exception:
            pass

    try:
        bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            reply_markup=markup,
        )
        log.info("Sent house card %s to chat %s.", house_id, chat_id)
    except Exception as exc:
        log.error("Failed to send house card: %s", exc)
        bot.send_message(
            chat_id,
            caption + "\n\n<i>(Photo unavailable)</i>",
            reply_markup=markup,
        )


# ─────────────────────────────────────────────────────────────
#  ADMIN — notify admin of new pending listing
# ─────────────────────────────────────────────────────────────

def notify_admin_new_listing(house: sqlite3.Row, landlord_id: int) -> None:
    """Send the admin a listing card with Approve / Reject buttons."""
    expiry_preview = (
        datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)
    ).strftime("%Y-%m-%d")

    category = house["category"] or "Residential"
    extra = ""
    if category == "Commercial":
        extra = (
            f"\n<b>📐 Dimensions :</b> {house['shop_dimensions'] or 'N/A'}\n"
            f"<b>💼 Best For   :</b> {house['suitable_for'] or 'N/A'}"
        )

    text = (
        "🔔 <b>New Listing Pending Approval</b>\n\n"
        f"<b>Landlord ID :</b> <code>{landlord_id}</code>\n"
        f"<b>House ID    :</b> <code>{house['id']}</code>\n"
        f"<b>🏷 Category  :</b> {category}\n"
        f"<b>📍 Area      :</b> {house['area']}\n"
        f"<b>💰 Rent      :</b> {house['rent_birr']:,} Birr / month\n"
        f"<b>📞 Phone     :</b> <code>{house['phone']}</code>"
        f"{extra}\n\n"
        f"<b>Description:</b>\n{house['description']}\n\n"
        f"<i>If approved, listing will be active until {expiry_preview}.</i>"
    )

    markup = admin_approval_markup(house["id"])

    photo = house["photo_file_id"]
    try:
        if photo:
            bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo,
                caption=text,
                reply_markup=markup,
            )
        else:
            bot.send_message(ADMIN_ID, text, reply_markup=markup)
        log.info("Admin notified about house %s.", house["id"])
    except Exception as exc:
        log.error("Failed to notify admin about house %s: %s", house["id"], exc)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ll_approve_"))
def cb_admin_approve_ll_manual(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ Not authorised.")
        return

    payload = call.data.replace("adm_ll_approve_", "")
    try:
        landlord_id_str, house_id = payload.split("_", 1)
        landlord_id = int(landlord_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid payload.")
        return

    expiry_date = (datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET status='pending_approval' WHERE id=?",
            (house_id,),
        )
        conn.commit()

    bot.answer_callback_query(call.id, "✅ Landlord manual payment approved — pending admin listing review.")
    bot.edit_message_caption(
        caption="✅ Approved (Manual) — Landlord payment verified. Listing set to pending admin review.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    # Notify landlord
    try:
        bot.send_message(
            landlord_id,
            "✅ Your manual payment was approved by admin.\n"
            "Your listing is now pending final admin review and will go live shortly."
        )
    except Exception:
        pass

    # Optionally notify final admin listing review using existing notify_admin_new_listing
    house = get_house_by_id(house_id)
    if house:
        notify_admin_new_listing(house, landlord_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ll_reject_"))
def cb_admin_reject_ll_manual(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ Not authorised.")
        return

    payload = call.data.replace("adm_ll_reject_", "")
    try:
        landlord_id_str, house_id = payload.split("_", 1)
        landlord_id = int(landlord_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid payload.")
        return

    bot.answer_callback_query(call.id, "❌ Rejected (Manual).")
    bot.edit_message_caption(
        caption="❌ Rejected (Manual) — Landlord payment proof not accepted.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    try:
        bot.send_message(
            landlord_id,
            "❌ Your manual payment proof was not accepted. Please retry or contact support."
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_rn_approve_"))
def cb_admin_approve_rn_manual(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ Not authorised.")
        return

    payload = call.data.replace("adm_rn_approve_", "")
    try:
        renter_id_str, house_id = payload.split("_", 1)
        renter_id = int(renter_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid payload.")
        return

    unlock_contact(renter_id, house_id)
    house = get_house_by_id(house_id)

    bot.answer_callback_query(call.id, "✅ Renter manual payment approved. Contact sent.")
    bot.edit_message_caption(
        caption="✅ Approved (Manual) — Renter payment verified. Contact unlocked.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    if house:
        _reveal_contact(renter_id, house)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_rn_reject_"))
def cb_admin_reject_rn_manual(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ Not authorised.")
        return

    payload = call.data.replace("adm_rn_reject_", "")
    try:
        renter_id_str, house_id = payload.split("_", 1)
        renter_id = int(renter_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid payload.")
        return

    bot.answer_callback_query(call.id, "❌ Rejected (Manual).")
    bot.edit_message_caption(
        caption="❌ Rejected (Manual) — Renter payment proof not accepted.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    try:
        bot.send_message(
            renter_id,
            "❌ Your manual payment proof was not accepted. Please retry or contact support."
        )
    except Exception:
        pass



# ─────────────────────────────────────────────────────────────
#  ADMIN CALLBACKS — Approve / Reject
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_approve_"))
def cb_admin_approve(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ You are not authorised.")
        return

    house_id    = call.data.replace("admin_approve_", "")
    expiry_date = (datetime.now() + timedelta(days=LISTING_ACTIVE_DAYS)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET status='active', expiry_date=? WHERE id=?",
            (expiry_date, house_id),
        )
        conn.commit()

    house = get_house_by_id(house_id)
    log.info("Admin approved house %s — active until %s.", house_id, expiry_date)

    bot.answer_callback_query(call.id, "✅ Listing approved and now live!")
    bot.edit_message_caption(
        caption=(
            f"✅ <b>APPROVED</b>\n\n"
            f"House <code>{house_id}</code> is now live until <b>{expiry_date}</b>."
        ),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    # Notify landlord
    if house:
        try:
            bot.send_message(
                house["landlord_id"],
                "🎉 <b>Your listing has been approved!</b>\n\n"
                f"<b>📍 Area:</b> {house['area']}\n"
                f"<b>💰 Rent:</b> {house['rent_birr']:,} Birr / month\n\n"
                f"Your property is now <b>live</b> and visible to all renters.\n"
                f"It will remain active until <b>{expiry_date}</b>.",
                reply_markup=landlord_main_menu(),
            )
        except Exception as exc:
            log.error("Could not notify landlord %s: %s", house["landlord_id"], exc)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_reject_"))
def cb_admin_reject(call: types.CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ You are not authorised.")
        return

    house_id = call.data.replace("admin_reject_", "")

    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET status='rejected' WHERE id=?",
            (house_id,),
        )
        conn.commit()

    house = get_house_by_id(house_id)
    log.info("Admin rejected house %s.", house_id)

    bot.answer_callback_query(call.id, "❌ Listing rejected.")
    bot.edit_message_caption(
        caption=f"❌ <b>REJECTED</b>\n\nHouse <code>{house_id}</code> has been rejected.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    # Notify landlord
    if house:
        try:
            bot.send_message(
                house["landlord_id"],
                "❌ <b>Your listing was not approved.</b>\n\n"
                f"<b>📍 Area:</b> {house['area']}\n\n"
                "Unfortunately your listing did not meet our guidelines.\n"
                f"Please contact {SUPPORT_USERNAME} for more information.",
                reply_markup=landlord_main_menu(),
            )
        except Exception as exc:
            log.error("Could not notify landlord %s: %s", house["landlord_id"], exc)


# ─────────────────────────────────────────────────────────────
#  /start COMMAND
# ─────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    upsert_user(message.from_user)
    user_id = message.from_user.id
    first   = message.from_user.first_name or "there"

    log.info("/start from user_id=%s username=%s", user_id, message.from_user.username)

    welcome = (
        f"<b>Welcome to BetLink, {first}!</b>\n\n"
        "Ethiopia's smartest property-matching bot.\n\n"
        "<b>How it works:</b>\n"
        "  🏠 Landlords — post a listing for <b>200 Birr</b>\n"
        "  🔍 Renters   — browse free; unlock contact for <b>100 Birr</b>\n\n"
        "Please choose your role to continue:"
    )
    bot.send_message(user_id, welcome, reply_markup=role_selection_markup())


# ─────────────────────────────────────────────────────────────
#  ROLE SELECTION CALLBACKS
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("role_"))
def cb_role_selection(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    role    = call.data.split("_", 1)[1]

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET role=? WHERE telegram_id=?", (role, user_id)
        )
        conn.commit()

    bot.answer_callback_query(call.id, f"Role set to {role.capitalize()}!")
    log.info("User %s chose role: %s", user_id, role)

    if role == "landlord":
        bot.edit_message_text(
            "<b>You are registered as a Landlord. 🏠</b>\n\n"
            "Use the menu below to manage your listings.",
            call.message.chat.id,
            call.message.message_id,
        )
        bot.send_message(
            user_id,
            "Here is your Landlord Menu:",
            reply_markup=landlord_main_menu(),
        )
    else:
        bot.edit_message_text(
            "<b>You are registered as a Renter. 🔍</b>\n\n"
            "Let's find your perfect home!",
            call.message.chat.id,
            call.message.message_id,
        )
        bot.send_message(
            user_id,
            "Use the menu below to browse listings or check your unlocked contacts:",
            reply_markup=renter_main_menu(),
        )


# ─────────────────────────────────────────────────────────────
#  LANDLORD MENU — Post a New House (upgraded flow)
# ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "[ 🏠 Post a New House ]")
def menu_post_house(message: types.Message) -> None:
    user_id = message.from_user.id
    log.info("Landlord %s started posting a new house.", user_id)
    user_state[user_id] = {"step": "category", "data": {}}

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🏠 Residential Home",  callback_data="ll_cat_Residential"),
        types.InlineKeyboardButton("🏢 Commercial Space",  callback_data="ll_cat_Commercial"),
    )
    bot.send_message(
        user_id,
        "<b>Step 1 — Property Category</b>\n\n"
        "What type of property are you listing?",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.send_message(user_id, "Choose a category:", reply_markup=kb)


# ── Category chosen by landlord ──
@bot.callback_query_handler(func=lambda c: c.data.startswith("ll_cat_"))
def cb_landlord_category(call: types.CallbackQuery) -> None:
    user_id  = call.from_user.id
    category = call.data.replace("ll_cat_", "")

    state = user_state.get(user_id)
    if not state:
        bot.answer_callback_query(call.id, "Session expired. Please start again.")
        return

    state["data"]["category"] = category
    bot.answer_callback_query(call.id, f"Category: {category}")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.edit_message_text(
        f"✅ Category set to <b>{category}</b>.",
        call.message.chat.id,
        call.message.message_id,
    )

    log.info("Landlord %s — category: %s", user_id, category)

    # Next step: location
    sent = bot.send_message(
        user_id,
        "<b>Step 2 — Location</b>\n\n"
        "Which neighbourhood / area is the property in?\n"
        "<i>Examples: Bole, Megenagna, Kazanchis, CMC, Ayat, Sarbet, Gerji</i>",
    )
    bot.register_next_step_handler(sent, step_collect_area)


def step_collect_area(message: types.Message) -> None:
    user_id = message.from_user.id
    area    = message.text.strip()
    if not area:
        bot.send_message(user_id, "Area cannot be empty. Please try again.")
        bot.register_next_step_handler(message, step_collect_area)
        return

    user_state[user_id]["data"]["area"] = area
    log.info("Landlord %s — area: %s", user_id, area)
    sent = bot.send_message(
        user_id,
        "<b>Step 3 — Monthly Rent</b>\n\n"
        "How much is the monthly rent in <b>Birr</b>?\n"
        "<i>Example: 8500</i>",
    )
    bot.register_next_step_handler(sent, step_collect_rent)


def step_collect_rent(message: types.Message) -> None:
    user_id = message.from_user.id
    try:
        rent = int(message.text.strip().replace(",", ""))
        if rent <= 0:
            raise ValueError("Rent must be positive.")
    except ValueError:
        sent = bot.send_message(
            user_id,
            "Please enter a valid number (e.g. <b>8500</b>). No letters or symbols.",
        )
        bot.register_next_step_handler(sent, step_collect_rent)
        return

    user_state[user_id]["data"]["rent_birr"] = rent
    log.info("Landlord %s — rent: %s Birr", user_id, rent)
    sent = bot.send_message(
        user_id,
        "<b>Step 5 — Description</b>\n\n"
        "Write a detailed description of the property.\n\n"
        "<b>Include:</b> number of rooms, floor, amenities, furnishing, etc.\n"
        "<b>Do NOT include</b> your phone number here — you will add that separately.",
    )
    bot.register_next_step_handler(sent, step_collect_description)


def step_collect_description(message: types.Message) -> None:
    user_id = message.from_user.id
    desc    = message.text.strip()
    if len(desc) < 10:
        sent = bot.send_message(
            user_id,
            "Description is too short. Please provide at least 10 characters."
        )
        bot.register_next_step_handler(sent, step_collect_description)
        return

    user_state[user_id]["data"]["description"] = desc
    log.info("Landlord %s — description saved.", user_id)
    sent = bot.send_message(
        user_id,
        "<b>Step 6 — Contact Phone Number</b>\n\n"
        "Enter your Ethiopian phone number.\n\n"
        "<b>Accepted formats:</b>\n"
        "  • <code>09XXXXXXXX</code>\n"
        "  • <code>07XXXXXXXX</code>\n"
        "  • <code>+2519XXXXXXXX</code>\n"
        "  • <code>+2517XXXXXXXX</code>\n\n"
        "<i>Example: 0911234567 or +251911234567</i>",
    )
    bot.register_next_step_handler(sent, step_collect_phone)


def step_collect_phone(message: types.Message) -> None:
    user_id = message.from_user.id
    phone   = message.text.strip().replace(" ", "").replace("-", "")

    if not validate_ethiopian_phone(phone):
        sent = bot.send_message(
            user_id,
            "❌ <b>Invalid phone number.</b>\n\n"
            "Please enter a valid Ethiopian mobile number:\n"
            "  • <code>09XXXXXXXX</code>  (e.g. 0911234567)\n"
            "  • <code>07XXXXXXXX</code>  (e.g. 0712345678)\n"
            "  • <code>+2519XXXXXXXX</code>\n"
            "  • <code>+2517XXXXXXXX</code>\n\n"
            "Try again:",
        )
        bot.register_next_step_handler(sent, step_collect_phone)
        return

    user_state[user_id]["data"]["phone"] = phone
    log.info("Landlord %s — phone: %s", user_id, phone)

    # Branch: commercial listings need extra steps
    category = user_state[user_id]["data"].get("category", "Residential")
    if category == "Commercial":
        sent = bot.send_message(
            user_id,
            "<b>Step 7 — Shop Dimensions</b>\n\n"
            "Please enter the dimensions of the commercial space in "
            "<b>Length x Width</b> format.\n\n"
            "<i>Example: 4x3, 6x4, or 5x4</i>",
        )
        bot.register_next_step_handler(sent, step_collect_dimensions)
    else:
        sent = bot.send_message(
            user_id,
            "<b>Step 7 — Upload Photo</b>\n\n"
            "Please send a clear photo of the property.\n"
            "<i>This will be the first thing renters see.</i>",
        )
        bot.register_next_step_handler(sent, step_collect_photo)


def step_collect_dimensions(message: types.Message) -> None:
    """Commercial-only step: collect shop dimensions."""
    user_id = message.from_user.id
    raw     = message.text.strip()

    m = DIMENSION_RE.match(raw)
    if not m:
        sent = bot.send_message(
            user_id,
            "❌ <b>Invalid format.</b>\n\n"
            "Please enter two numbers separated by an 'x'.\n"
            "<i>Example: 4x3, 6x4, or 5x4</i>",
        )
        bot.register_next_step_handler(sent, step_collect_dimensions)
        return

    dims = f"{m.group(1)}x{m.group(2)}"
    user_state[user_id]["data"]["shop_dimensions"] = dims
    log.info("Landlord %s — dimensions: %s", user_id, dims)

    # Show the business-type multi-select keyboard
    selected: list[str] = []
    user_state[user_id]["data"]["suitable_for_selected"] = selected
    user_state[user_id]["step"] = "biz_type"

    bot.send_message(
        user_id,
        "<b>Step 8 — Business Types</b>\n\n"
        "For what type of business is this shop best suited?\n"
        "<i>(Example: Cafe, Boutique, Electronics, Pool Game, or write your own/Other)</i>\n\n"
        "Tap to <b>toggle</b> one or more options, then tap <b>Done Selecting</b>.",
        reply_markup=_build_business_type_kb(selected),
    )


# ── Toggle a business type ──
@bot.callback_query_handler(func=lambda c: c.data.startswith("biz_toggle_"))
def cb_biz_toggle(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    state   = user_state.get(user_id, {})

    if state.get("step") != "biz_type":
        bot.answer_callback_query(call.id, "No active session.")
        return

    idx = int(call.data.replace("biz_toggle_", ""))
    if idx < 0 or idx >= len(BUSINESS_TYPES):
        bot.answer_callback_query(call.id, "Invalid option.")
        return

    bt       = BUSINESS_TYPES[idx]
    selected = state["data"].get("suitable_for_selected", [])

    if bt in selected:
        selected.remove(bt)
        bot.answer_callback_query(call.id, f"Removed: {bt}")
    else:
        selected.append(bt)
        bot.answer_callback_query(call.id, f"Added: {bt}")

    state["data"]["suitable_for_selected"] = selected
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_build_business_type_kb(selected),
    )


# ── Done selecting business types ──
@bot.callback_query_handler(func=lambda c: c.data == "biz_done")
def cb_biz_done(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    state   = user_state.get(user_id, {})

    if state.get("step") != "biz_type":
        bot.answer_callback_query(call.id, "No active session.")
        return

    selected = state["data"].get("suitable_for_selected", [])
    if not selected:
        bot.answer_callback_query(call.id, "Please select at least one business type!", show_alert=True)
        return

    suitable_str = ", ".join(selected)
    state["data"]["suitable_for"] = suitable_str
    state["step"] = "photo"
    bot.answer_callback_query(call.id, "Business types saved!")

    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=None,
    )
    bot.edit_message_text(
        f"✅ <b>Business Types:</b> {suitable_str}",
        call.message.chat.id,
        call.message.message_id,
    )

    sent = bot.send_message(
        user_id,
        "<b>Step 9 — Upload Photo</b>\n\n"
        "Please send a clear photo of the commercial space.\n"
        "<i>This will be the first thing renters see.</i>",
    )
    bot.register_next_step_handler(sent, step_collect_photo)


def step_collect_photo(message: types.Message) -> None:
    user_id = message.from_user.id

    if message.content_type != "photo":
        sent = bot.send_message(
            user_id,
            "Please send an actual <b>photo</b> (not a file or text).",
        )
        bot.register_next_step_handler(sent, step_collect_photo)
        return

    photo_file_id = message.photo[-1].file_id
    user_state[user_id]["data"]["photo_file_id"] = photo_file_id
    log.info("Landlord %s — photo captured: %s", user_id, photo_file_id)

    data     = user_state[user_id]["data"]
    category = data.get("category", "Residential")

    # Build summary
    extra_lines = ""
    if category == "Commercial":
        extra_lines = (
            f"\n  📐 Dimensions : {data.get('shop_dimensions', 'N/A')}"
            f"\n  💼 Best For   : {data.get('suitable_for', 'N/A')}"
        )

    summary = (
        "<b>📋 Listing Summary</b>\n\n"
        f"  🏷 Category  : {category}\n"
        f"  📍 Location  : {data['area']}\n"
        f"  💰 Rent      : {data['rent_birr']:,} Birr / month\n"
        f"  📞 Phone     : {data['phone']}\n"
        f"  🖼 Photo     : Received ✅"
        f"{extra_lines}\n\n"
        f"<b>Listing Fee: {LISTING_FEE_BIRR} Birr</b>\n\n"
        "To submit your listing for approval, please complete the payment below.\n"
        "After payment, an admin will review and activate your listing within 24 hours.\n\n"
        "You will be redirected to <b>Chapa / Telebirr</b>."
    )

    # Create pending house entry in DB
    house_id = str(uuid.uuid4())[:8]
    user_state[user_id]["pending_house_id"] = house_id

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO houses
              (id, landlord_id, area, rent_birr, description,
               photo_file_id, phone, status,
               category, shop_dimensions, suitable_for, neighborhood)
            VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?)
            """,
            (
                house_id,
                user_id,
                data["area"],
                data["rent_birr"],
                data["description"],
                data["photo_file_id"],
                data["phone"],
                category,
                data.get("shop_dimensions"),
                data.get("suitable_for"),
                data.get("neighborhood"),
            ),
        )
        conn.commit()

        log.info("Pending house %s created for landlord %s.", house_id, user_id)

    # Generate unique tx_ref and real Chapa checkout link
    tx_ref       = f"landlord_{house_id}_{int(time.time())}"
    landlord_url = get_real_chapa_link(tx_ref, LISTING_FEE_BIRR, "BetLink Listing Fee")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text="⚡ Instant Pay (Telebirr / CBE via Chapa)",
            url=landlord_url,
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🏦 Manual Bank Transfer (Direct CBE / Telebirr)",
            callback_data=f"ll_manual_{house_id}_{tx_ref}",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "[ 🔄 Verify My Payment ]",
            callback_data=f"landlord_pay_success_{house_id}_{tx_ref}",
        )
    )

    bot.send_message(user_id, summary, reply_markup=kb)



# ─────────────────────────────────────────────────────────────
#  LANDLORD PAYMENT SUCCESS CALLBACK
# ─────────────────────────────────────────────────────────────


@bot.callback_query_handler(func=lambda c: c.data.startswith("ll_manual_"))
def cb_landlord_manual_start(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    payload = call.data.replace("ll_manual_", "")
    parts = payload.split("_", 1)
    if len(parts) != 2:
        bot.answer_callback_query(call.id, "Invalid data.")
        return
    house_id, tx_ref = parts

    user_state[user_id] = {"step": "ll_manual_upload", "data": {"house_id": house_id, "tx_ref": tx_ref}}
    bot.answer_callback_query(call.id)
    bot.send_message(
        user_id,
        "Please transfer the fee to:\n"
        f"🏦 CBE: {CBE_ACCOUNT}\n"
        f"📱 Telebirr: {TELEBIRR_ACCOUNT}\n\n"
        "⚠️ Send a clear SCREENSHOT or PHOTO of your bank receipt right here as proof."
    )


@bot.message_handler(content_types=["photo"])
def handle_manual_screenshot(message: types.Message) -> None:
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if not state:
        # This photo is not part of a manual flow; ignore here and let other handlers work if any.
        return

    step = state.get("step")
    if step not in ("ll_manual_upload", "rn_manual_upload"):
        return

    file_id = message.photo[-1].file_id

    if step == "ll_manual_upload":
        house_id = state["data"]["house_id"]
        tx_ref = state["data"]["tx_ref"]

        # Build admin review with Approve/Reject
        house = get_house_by_id(house_id)
        if not house:
            bot.send_message(user_id, "Listing not found. Please restart.")
            return

        text = (
            "🧾 <b>Manual Payment — Landlord</b>\n\n"
            f"User ID: <code>{user_id}</code>\n"
            f"House ID: <code>{house_id}</code>\n"
            f"Tx Ref: <code>{tx_ref}</code>\n"
            f"Area: {house['area']}\n"
            f"Rent: {house['rent_birr']:,} Birr/mo\n\n"
            "Attached: Payment screenshot."
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"adm_ll_approve_{user_id}_{house_id}"),
            types.InlineKeyboardButton("❌ Reject",  callback_data=f"adm_ll_reject_{user_id}_{house_id}"),
        )
        try:
            bot.send_photo(ADMIN_ID, file_id, caption=text, reply_markup=kb)
        except Exception:
            bot.send_message(ADMIN_ID, text, reply_markup=kb)

        bot.send_message(user_id, "Thanks! Your payment proof was sent to admin. You will be notified after review.")
        user_state.pop(user_id, None)

    elif step == "rn_manual_upload":
        data = state["data"]
        house_id = data["house_id"]
        tx_ref   = data["tx_ref"]

        house = get_house_by_id(house_id)
        if not house:
            bot.send_message(user_id, "Listing not found. Please restart.")
            return

        text = (
            "🧾 <b>Manual Payment — Renter</b>\n\n"
            f"Renter ID: <code>{user_id}</code>\n"
            f"Requested House ID: <code>{house_id}</code>\n"
            f"Tx Ref: <code>{tx_ref}</code>\n"
            f"Area: {house['area']}\n"
            f"Rent: {house['rent_birr']:,} Birr/mo\n\n"
            "Attached: Payment screenshot.\n\n"
            "If approved, send the landlord contact to the renter instantly."
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"adm_rn_approve_{user_id}_{house_id}"),
            types.InlineKeyboardButton("❌ Reject",  callback_data=f"adm_rn_reject_{user_id}_{house_id}"),
        )
        try:
            bot.send_photo(ADMIN_ID, file_id, caption=text, reply_markup=kb)
        except Exception:
            bot.send_message(ADMIN_ID, text, reply_markup=kb)

        bot.send_message(user_id, "Thanks! Your payment proof was sent to admin. You will be notified after review.")
        user_state.pop(user_id, None)


@bot.callback_query_handler(
    func=lambda c: c.data.startswith("landlord_pay_success_")
)
def cb_landlord_payment_success(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id

    # callback_data format: "landlord_pay_success_{house_id}_{tx_ref}"
    payload = call.data.replace("landlord_pay_success_", "")
    parts   = payload.split("_", 1)

    if len(parts) != 2:
        bot.answer_callback_query(call.id, "Invalid payment data. Contact support.")
        return

    house_id, tx_ref = parts

    bot.answer_callback_query(call.id, "Verifying payment with Chapa...")

    if not verify_chapa_payment(tx_ref, expected_amount=LISTING_FEE_BIRR):
        log.warning(
            "Landlord %s payment verification FAILED [house=%s, tx_ref=%s].",
            user_id, house_id, tx_ref,
        )
        bot.send_message(
            user_id,
            "<b>Payment Not Confirmed</b>\n\n"
            f"Chapa could not verify your payment of <b>{LISTING_FEE_BIRR} Birr</b>.\n\n"
            "Please complete the payment using the link above, then tap "
            "<b>Verify My Payment</b> again.\n\n"
            f"If you believe this is an error, contact {SUPPORT_USERNAME}.",
        )
        return

    # Payment confirmed — set to pending_approval (NOT active yet)
    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET status='pending_approval' WHERE id=? AND landlord_id=?",
            (house_id, user_id),
        )
        conn.commit()

    log.info(
        "Landlord %s payment verified — house %s is now PENDING ADMIN APPROVAL.",
        user_id, house_id,
    )

    bot.edit_message_text(
        "✅ <b>Payment Confirmed!</b>\n\n"
        "Your listing has been submitted and is <b>pending admin review</b>.\n\n"
        "You will receive a notification once it is approved and goes live.\n"
        "This usually takes less than 24 hours.",
        call.message.chat.id,
        call.message.message_id,
    )
    bot.send_message(
        user_id,
        "Here is your Landlord Menu:",
        reply_markup=landlord_main_menu(),
    )
    user_state.pop(user_id, None)

    # Notify admin
    house = get_house_by_id(house_id)
    if house:
        notify_admin_new_listing(house, user_id)


# ─────────────────────────────────────────────────────────────
#  LANDLORD MENU — "My Active Listings"
# ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "[ 📋 My Active Listings ]")
def menu_my_listings(message: types.Message) -> None:
    user_id = message.from_user.id
    houses  = get_landlord_houses(user_id)

    if not houses:
        bot.send_message(
            user_id,
            "You have no listings yet.\n"
            "Tap <b>[ 🏠 Post a New House ]</b> to create your first one.",
        )
        return

    bot.send_message(user_id, f"<b>Your Listings ({len(houses)} found)</b>")

    for h in houses:
        status_map = {
            "active":           "🟢 ACTIVE",
            "pending":          "🟡 Pending Payment",
            "pending_approval": "🟠 Pending Admin Review",
            "rejected":         "🔴 Rejected",
        }
        status_label = status_map.get(h["status"], h["status"].upper())
        expiry   = h["expiry_date"] or "N/A"
        category = h["category"] or "Residential"
        cat_icon = "🏢" if category == "Commercial" else "🏠"

        extra = ""
        if category == "Commercial":
            extra = (
                f"\nDimensions : {h['shop_dimensions'] or 'N/A'}"
                f"\nBest For   : {h['suitable_for'] or 'N/A'}"
            )

        text = (
            f"<b>{cat_icon} {h['area']}</b> — {h['rent_birr']:,} Birr/mo\n"
            f"Category : {category}\n"
            f"Status   : {status_label}\n"
            f"Expires  : {expiry}\n"
            f"ID       : <code>{h['id']}</code>"
            f"{extra}"
        )

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                "🗑️ Delete / Deactivate",
                callback_data=f"landlord_delete_{h['id']}",
            )
        )
        bot.send_message(user_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("landlord_delete_"))
def cb_landlord_delete_listing(call: types.CallbackQuery) -> None:
    user_id  = call.from_user.id
    house_id = call.data.replace("landlord_delete_", "")

    house = get_house_by_id(house_id)
    if not house or house["landlord_id"] != user_id:
        bot.answer_callback_query(call.id, "Listing not found or access denied.")
        return

    with get_conn() as conn:
        conn.execute(
            "UPDATE houses SET status='inactive' WHERE id=? AND landlord_id=?",
            (house_id, user_id),
        )
        conn.commit()

    log.info("Landlord %s deactivated house %s.", user_id, house_id)
    bot.answer_callback_query(call.id, "Listing deactivated.")
    bot.edit_message_text(
        f"🗑️ <b>Listing Deactivated</b>\n\n"
        f"House <code>{house_id}</code> in <b>{house['area']}</b> has been removed "
        f"from the renter browsing feed.",
        call.message.chat.id,
        call.message.message_id,
    )


# ─────────────────────────────────────────────────────────────
#  LANDLORD MENU — Contact Support
# ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "[ 📞 Contact Support ]")
def menu_support(message: types.Message) -> None:
    bot.send_message(
        message.from_user.id,
        f"For support, please reach out to our team:\n\n"
        f"  Telegram: {SUPPORT_USERNAME}\n"
        f"  Hours   : Mon-Sat, 09:00-18:00 EAT\n\n"
        f"We typically respond within 2 hours.",
    )


# ─────────────────────────────────────────────────────────────
#  RENTER BROWSING — start flow (with category selector)
# ─────────────────────────────────────────────────────────────

@bot.message_handler(commands=["browse"])
def cmd_browse(message: types.Message) -> None:
    upsert_user(message.from_user)
    _ask_renter_category(message.from_user.id)


@bot.message_handler(func=lambda m: m.text == "[ 🔍 Browse Listings ]")
def menu_browse(message: types.Message) -> None:
    upsert_user(message.from_user)
    _ask_renter_category(message.from_user.id)


def _ask_renter_category(user_id: int) -> None:
    bot.send_message(
        user_id,
        "<b>What are you looking for?</b>\n\n"
        "Choose the type of property you want to browse:",
        reply_markup=renter_category_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("browse_cat_"))
def cb_renter_browse_category(call: types.CallbackQuery) -> None:
    user_id  = call.from_user.id
    category = call.data.replace("browse_cat_", "")

    bot.answer_callback_query(call.id)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    if category == "Commercial":
        # Ask business types first (unchanged path), but AFTER that we will also ask for neighborhood
        user_state[user_id] = {"step": "renter_biz_filter", "data": {"biz_filter_selected": [], "category": "Commercial"}}
        bot.send_message(
            user_id,
            "<b>What kind of business do you want to open?</b>\n\n"
            "Select the business types you are interested in.\n"
            "<i>(Example: Cafe, Boutique, Electronics, Pool Game, or choose Other/Any)</i>\n\n"
            "Tap to toggle, then tap <b>Done Selecting</b>.",
            reply_markup=_build_business_type_kb([]),
        )
        user_state[user_id]["step"] = "renter_biz_filter"
    else:
        # Residential or All
        cat_arg = None if category == "All" else category
        # Save chosen category in state and prompt neighborhood
        user_state[user_id] = {"step": "renter_nb_pick", "data": {"category": cat_arg}}
        bot.send_message(
            user_id,
            "<b>Select a neighborhood filter</b>\n\n"
            "Choose a zone to narrow results, or pick Everywhere to view all.",
            reply_markup=renter_neighborhood_markup(),
        )


# ─────────────────────────────────────────────────────────────
#  RENTER NEIGHBORHOOD SELECTION HANDLER  (was missing entirely)
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("nb_"))
def cb_neighborhood_selected(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    nb_raw  = call.data.replace("nb_", "")          # "Bole", "CMC", "Everywhere" …
    state   = user_state.get(user_id, {})
    step    = state.get("step", "")

    bot.answer_callback_query(call.id)
    bot.edit_message_reply_markup(
        call.message.chat.id, call.message.message_id, reply_markup=None
    )

    # "Everywhere" → no neighbourhood filter
    neighborhood = None if nb_raw == "Everywhere" else nb_raw

    if step == "renter_nb_pick":
        # Path: Residential or All  →  neighbourhood chosen
        category = state.get("data", {}).get("category")   # None means All
        user_state.pop(user_id, None)
        _start_renter_browse(user_id, category=category, neighborhood=neighborhood)

    elif step == "renter_nb_pick_after_biz":
        # Path: Commercial  →  business types chosen  →  neighbourhood chosen
        biz_selected = state.get("data", {}).get("biz_filter_selected", [])
        user_state.pop(user_id, None)

        houses = get_commercial_houses_by_business(biz_selected, neighborhood=neighborhood)
        if not houses:
            loc_label = f" in <b>{neighborhood}</b>" if neighborhood else ""
            bot.send_message(
                user_id,
                f"😔 No commercial listings found{loc_label} matching your interests.\n\n"
                "Check back soon — new spaces are added regularly!",
            )
            return

        total = len(houses)
        user_state[user_id] = {
            "browse_index": 0,
            "browse_list": [h["id"] for h in houses],
        }
        send_house_card(user_id, houses[0], index=1, total=total)

    else:
        # Stale / unknown state — do a general browse
        user_state.pop(user_id, None)
        _start_renter_browse(user_id, neighborhood=neighborhood)


# ── Renter business-type filter toggles ──
@bot.callback_query_handler(func=lambda c: c.data == "biz_done")
def cb_renter_biz_filter_done(call: types.CallbackQuery) -> None:
    """
    Shared handler. If renter is in biz filter flow, next ask for neighborhood.
    If landlord is in biz_type, handled elsewhere.
    """
    user_id = call.from_user.id
    state   = user_state.get(user_id, {})

    if state.get("step") == "renter_biz_filter":
        selected = state["data"].get("biz_filter_selected", [])
        if not selected:
            bot.answer_callback_query(call.id, "Please select at least one type!", show_alert=True)
            return

        # Save and move to neighborhood picker
        state["data"]["biz_filter_selected"] = selected
        state["step"] = "renter_nb_pick_after_biz"  # new step
        bot.answer_callback_query(call.id, "Great — now pick a neighborhood.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.edit_message_text(
            f"Selected business interests: <b>{', '.join(selected)}</b>\n\n"
            "Now choose a neighborhood filter:",
            call.message.chat.id,
            call.message.message_id,
        )
        bot.send_message(
            user_id,
            "<b>Select a neighborhood filter</b>\n\n"
            "Choose a zone to narrow results, or pick Everywhere to view all.",
            reply_markup=renter_neighborhood_markup(),
        )
        return


# ── Toggle for renter business filter ──
# We reuse the same biz_toggle_ handler but check state step
@bot.callback_query_handler(func=lambda c: c.data.startswith("biz_toggle_"))
def cb_biz_toggle_shared(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    state   = user_state.get(user_id, {})
    step    = state.get("step", "")

    idx = int(call.data.replace("biz_toggle_", ""))
    if idx < 0 or idx >= len(BUSINESS_TYPES):
        bot.answer_callback_query(call.id, "Invalid option.")
        return

    bt = BUSINESS_TYPES[idx]

    if step == "renter_biz_filter":
        selected = state["data"].get("biz_filter_selected", [])
        if bt in selected:
            selected.remove(bt)
            bot.answer_callback_query(call.id, f"Removed: {bt}")
        else:
            selected.append(bt)
            bot.answer_callback_query(call.id, f"Added: {bt}")
        state["data"]["biz_filter_selected"] = selected
    elif step == "biz_type":
        # Landlord toggle
        selected = state["data"].get("suitable_for_selected", [])
        if bt in selected:
            selected.remove(bt)
            bot.answer_callback_query(call.id, f"Removed: {bt}")
        else:
            selected.append(bt)
            bot.answer_callback_query(call.id, f"Added: {bt}")
        state["data"]["suitable_for_selected"] = selected
    else:
        bot.answer_callback_query(call.id, "No active session.")
        return

    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_build_business_type_kb(selected),
    )


def _start_renter_browse(user_id: int,
                          category: str | None = None,
                          neighborhood: str | None = None) -> None:
    houses = get_active_houses(category=category, neighborhood=neighborhood)
    if not houses:
        label = ""
        if neighborhood:
            label += f" in <b>{neighborhood}</b>"
        if category:
            label += f" ({category})"
        bot.send_message(
            user_id,
            f"😔 No listings found{label} right now.\n\nTry a different area or check back soon!",
        )
        return

    total = len(houses)
    user_state[user_id] = {
        "browse_index": 0,
        "browse_list": [h["id"] for h in houses],
    }
    send_house_card(user_id, houses[0], index=1, total=total)


# ─────────────────────────────────────────────────────────────
#  RENTER NAVIGATION — Prev / Next callbacks
# ─────────────────────────────────────────────────────────────

def _get_browse_houses(user_id: int) -> list[sqlite3.Row]:
    """Reconstruct the current browse list from state or fall back to all active."""
    state = user_state.get(user_id, {})
    id_list = state.get("browse_list")
    if id_list:
        rows = [get_house_by_id(hid) for hid in id_list]
        return [r for r in rows if r is not None]
    return get_active_houses()


@bot.callback_query_handler(func=lambda c: c.data.startswith("prev_house_"))
def cb_prev_house(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    houses  = _get_browse_houses(user_id)
    total   = len(houses)

    if total == 0:
        bot.answer_callback_query(call.id, "No listings available.")
        return

    current_index = user_state.get(user_id, {}).get("browse_index", 0)
    new_index     = (current_index - 1) % total
    user_state.setdefault(user_id, {})["browse_index"] = new_index

    bot.answer_callback_query(call.id)
    send_house_card(
        user_id,
        houses[new_index],
        index=new_index + 1,
        total=total,
        message_id_to_edit=call.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("next_house_"))
def cb_next_house(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    houses  = _get_browse_houses(user_id)
    total   = len(houses)

    if total == 0:
        bot.answer_callback_query(call.id, "No listings available.")
        return

    current_index = user_state.get(user_id, {}).get("browse_index", 0)
    new_index     = (current_index + 1) % total
    user_state.setdefault(user_id, {})["browse_index"] = new_index

    bot.answer_callback_query(call.id)
    send_house_card(
        user_id,
        houses[new_index],
        index=new_index + 1,
        total=total,
        message_id_to_edit=call.message.message_id,
    )


# ─────────────────────────────────────────────────────────────
#  RENTER MENU — "My Unlocked Contacts"
# ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "[ 📋 My Unlocked Contacts ]")
def menu_my_unlocked(message: types.Message) -> None:
    user_id = message.from_user.id
    houses  = get_unlocked_houses_for_renter(user_id)

    if not houses:
        bot.send_message(
            user_id,
            "You haven't unlocked any contacts yet.\n\n"
            "Tap <b>[ 🔍 Browse Listings ]</b> to find a property and unlock a landlord's number.",
        )
        return

    bot.send_message(
        user_id,
        f"<b>📋 Your Unlocked Contacts ({len(houses)} total)</b>\n\n"
        "Here are all the landlords you've already paid to access:",
    )

    for h in houses:
        status_note = ""
        if h["status"] == "inactive":
            status_note = "\n⚠️ <i>This listing has been deactivated by the landlord.</i>"
        elif h["expiry_date"] and h["expiry_date"] < datetime.now().strftime("%Y-%m-%d"):
            status_note = "\n⚠️ <i>This listing has expired.</i>"

        category = h["category"] or "Residential"
        extra = ""
        if category == "Commercial":
            dims_raw = h["shop_dimensions"] or ""
            m = DIMENSION_RE.match(dims_raw)
            dims_display = f"{m.group(1)}m x {m.group(2)}m" if m else dims_raw
            extra = (
                f"\n<b>📐 Dimensions:</b> {dims_display}"
                f"\n<b>💼 Best For:</b> {h['suitable_for'] or 'N/A'}"
            )

        text = (
            "<b>🏠 Unlocked Contact</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>🏷 Category :</b> {category}\n"
            f"<b>📍 Location :</b> {h['area']}\n"
            f"<b>💰 Rent     :</b> {h['rent_birr']:,} Birr / month\n"
            f"{extra}\n\n"
            f"<b>📝 Description:</b>\n{h['description']}\n\n"
            f"<b>📞 Phone    :</b> <code>{h['phone']}</code>\n"
            f"{status_note}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        bot.send_message(user_id, text)


# ─────────────────────────────────────────────────────────────
#  RENTER UNLOCK — Step 1: show payment prompt (paywall trigger)
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("rn_manual_"))
def cb_renter_manual_start(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    payload = call.data.replace("rn_manual_", "")
    parts = payload.split("_", 1)
    if len(parts) != 2:
        bot.answer_callback_query(call.id, "Invalid data.")
        return
    house_id, tx_ref = parts

    user_state[user_id] = {"step": "rn_manual_upload", "data": {"house_id": house_id, "tx_ref": tx_ref}}
    bot.answer_callback_query(call.id)
    bot.send_message(
        user_id,
        "Please transfer the fee to:\n"
        f"🏦 CBE: {CBE_ACCOUNT}\n"
        f"📱 Telebirr: {TELEBIRR_ACCOUNT}\n\n"
        "⚠️ Send a clear SCREENSHOT or PHOTO of your bank receipt right here as proof."
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("unlock_id_"))
def cb_unlock_contact(call: types.CallbackQuery) -> None:
    user_id  = call.from_user.id
    house_id = call.data.replace("unlock_id_", "")
    house    = get_house_by_id(house_id)

    if not house:
        bot.answer_callback_query(call.id, "Listing not found.")
        return

    # Already unlocked — show contact instantly for free (they already paid)
    if is_contact_unlocked(user_id, house_id):
        bot.answer_callback_query(call.id, "You have already unlocked this contact.")
        _reveal_contact(user_id, house)
        return

    log.info("Renter %s requesting unlock for house %s.", user_id, house_id)
    bot.answer_callback_query(call.id)

       # Generate unique tx_ref and real Chapa checkout link
    tx_ref     = f"renter_{house_id}_{int(time.time())}"
    renter_url = get_real_chapa_link(tx_ref, UNLOCK_FEE_BIRR, "BetLink Unlock Contact")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text="⚡ Instant Pay (Telebirr / CBE via Chapa)",
            url=renter_url,
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🏦 Manual Bank Transfer (Direct CBE / Telebirr)",
            callback_data=f"rn_manual_{house_id}_{tx_ref}",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "[ 🔄 Verify My Payment ]",
            callback_data=f"renter_pay_success_{house_id}_{tx_ref}",
        )
    )


    bot.send_message(
        user_id,
        f"<b>🔓 Unlock Landlord Contact</b>\n\n"
        f"Property in <b>{house['area']}</b> — {house['rent_birr']:,} Birr/mo\n\n"
        f"Pay <b>{UNLOCK_FEE_BIRR} Birr</b> to reveal the landlord's "
        f"phone number.\n\n"
        "1️⃣ Tap <b>Pay</b> to open Chapa and complete the payment.\n"
        "2️⃣ Come back here and tap <b>Verify My Payment</b>.",
        reply_markup=kb,
    )


# ─────────────────────────────────────────────────────────────
#  RENTER UNLOCK — Step 2: verify payment & reveal contact
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(
    func=lambda c: c.data.startswith("renter_pay_success_")
)
def cb_renter_payment_success(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id

    # callback_data format: "renter_pay_success_{house_id}_{tx_ref}"
    payload = call.data.replace("renter_pay_success_", "")
    parts   = payload.split("_", 1)

    if len(parts) != 2:
        bot.answer_callback_query(call.id, "Invalid payment data. Contact support.")
        return

    house_id, tx_ref = parts
    house = get_house_by_id(house_id)

    if not house:
        bot.answer_callback_query(call.id, "Listing not found.")
        return

    bot.answer_callback_query(call.id, "Verifying payment with Chapa...")

    if verify_chapa_payment(tx_ref, expected_amount=UNLOCK_FEE_BIRR):
        unlock_contact(user_id, house_id)
        log.info("Renter %s unlocked contact for house %s.", user_id, house_id)

        bot.edit_message_text(
            "✅ <b>Payment Confirmed!</b>\n\n"
            "The landlord's contact information has been sent to you below.",
            call.message.chat.id,
            call.message.message_id,
        )
        _reveal_contact(user_id, house)
    else:
        log.warning(
            "Renter %s payment verification FAILED [house=%s, tx_ref=%s].",
            user_id, house_id, tx_ref,
        )
        bot.answer_callback_query(
            call.id,
            "⚠️ Payment not found yet. Please pay via Telebirr first!",
            show_alert=True,
        )
        bot.send_message(
            user_id,
            "<b>⚠️ Payment Not Confirmed</b>\n\n"
            f"Chapa could not verify your payment of <b>{UNLOCK_FEE_BIRR} Birr</b>.\n\n"
            "Please complete the payment using the link above, then tap "
            "<b>🔄 Verify My Payment</b> again.\n\n"
            f"If you believe this is an error, contact {SUPPORT_USERNAME}.",
        )


def _reveal_contact(user_id: int, house: sqlite3.Row) -> None:
    category = house["category"] or "Residential"
    extra = ""
    if category == "Commercial":
        dims_raw = house["shop_dimensions"] or ""
        m = DIMENSION_RE.match(dims_raw)
        dims_display = f"{m.group(1)}m x {m.group(2)}m" if m else dims_raw
        extra = (
            f"\n<b>📐 Dimensions:</b> {dims_display}"
            f"\n<b>💼 Best For  :</b> {house['suitable_for'] or 'N/A'}"
        )

    msg = (
        "<b>📞 Landlord Contact — Unlocked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>🏷 Category :</b> {category}\n"
        f"<b>📍 Location :</b> {house['area']}\n"
        f"<b>💰 Rent     :</b> {house['rent_birr']:,} Birr / month\n"
        f"{extra}\n\n"
        f"<b>📝 Description:</b>\n{house['description']}\n\n"
        f"<b>📞 Phone    :</b> <code>{house['phone']}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Please contact the landlord directly to schedule a viewing.\n"
        "BetLink is not responsible for the accuracy of listings.</i>"
    )
    bot.send_message(user_id, msg)
    log.info("Contact for house %s revealed to renter %s.", house["id"], user_id)


# ─────────────────────────────────────────────────────────────
#  FALLBACK — unknown messages
# ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True)
def fallback_handler(message: types.Message) -> None:
    upsert_user(message.from_user)
    bot.send_message(
        message.from_user.id,
        "I didn't understand that.\n\n"
        "Use /start to set your role, or /browse to view listings.",
    )


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    log.info("BetLink Bot is starting — polling for updates...")
    bot.infinity_polling(
        timeout=10,
        long_polling_timeout=5,
        logger_level=logging.WARNING,
    )