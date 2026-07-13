import hashlib
import hmac
import html
import json
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit_js_eval import streamlit_js_eval


APP_TITLE = "共通機器予約システム（愛知学院大学薬学部）"
DB_PATH = "equipment_booking.db"

SYSTEM_ADMIN_USERNAME = "admin"
SYSTEM_ADMIN_PASSWORD = "1234"
APP_ACCESS_CODE = "agupharma"

SLOT_MINUTES = 15
SLOT_HEIGHT = 24
MOBILE_BREAKPOINT = 768
JST = ZoneInfo("Asia/Tokyo")

st.set_page_config(page_title=APP_TITLE, page_icon="🧪", layout="wide")

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; padding-bottom: 3rem;}
    div[data-testid="stButton"] button {min-height: 42px;}
    @media (max-width: 768px) {
        .block-container {padding-top: .8rem; padding-left: .8rem; padding-right: .8rem;}
        h1 {font-size: 1.55rem !important; line-height: 1.25 !important;}
        h2 {font-size: 1.35rem !important;}
        h3 {font-size: 1.15rem !important;}
        div[data-testid="stButton"] button,
        div[data-testid="stFormSubmitButton"] button {width: 100%; min-height: 46px;}
        input {min-height: 42px;}
        textarea {min-height: 90px;}
        [data-testid="stSidebar"] {min-width: 260px;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Database
# ============================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def migrate_instrument_managers_schema(conn: sqlite3.Connection) -> None:
    """Migrate the legacy manager_email schema to the current manager_id schema."""
    columns = table_columns(conn, "instrument_managers")

    if {"instrument_id", "manager_id"}.issubset(columns):
        return

    if "manager_email" not in columns:
        raise RuntimeError(
            "instrument_managers テーブルの構造を認識できません。"
        )

    backup_exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'instrument_managers_legacy_email'
        """
    ).fetchone()

    if backup_exists is None:
        conn.execute(
            """
            CREATE TABLE instrument_managers_legacy_email AS
            SELECT *
            FROM instrument_managers
            """
        )

    conn.execute("DROP TABLE IF EXISTS instrument_managers_new")

    conn.execute(
        """
        CREATE TABLE instrument_managers_new (
            instrument_id INTEGER NOT NULL,
            manager_id INTEGER NOT NULL,
            UNIQUE(instrument_id, manager_id),
            FOREIGN KEY (instrument_id)
                REFERENCES instruments(id) ON DELETE CASCADE,
            FOREIGN KEY (manager_id)
                REFERENCES manager_accounts(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO instrument_managers_new (
            instrument_id,
            manager_id
        )
        SELECT
            legacy.instrument_id,
            accounts.id
        FROM instrument_managers legacy
        JOIN manager_accounts accounts
            ON lower(accounts.username) = lower(legacy.manager_email)
        """
    )

    conn.execute("DROP TABLE instrument_managers")
    conn.execute(
        """
        ALTER TABLE instrument_managers_new
        RENAME TO instrument_managers
        """
    )


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS instruments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                notice TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS manager_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS system_admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS instrument_managers (
                instrument_id INTEGER NOT NULL,
                manager_id INTEGER NOT NULL,
                UNIQUE(instrument_id, manager_id),
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id) ON DELETE CASCADE,
                FOREIGN KEY (manager_id)
                    REFERENCES manager_accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS custom_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                field_type TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 0,
                options_json TEXT NOT NULL DEFAULT '[]',
                display_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                affiliation TEXT NOT NULL,
                reservation_date TEXT NOT NULL DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                purpose TEXT NOT NULL,
                purpose_other TEXT NOT NULL DEFAULT '',
                remarks TEXT NOT NULL DEFAULT '',
                pin_salt TEXT NOT NULL,
                pin_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reservation_field_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                custom_field_id INTEGER,
                field_name_snapshot TEXT NOT NULL,
                field_type_snapshot TEXT NOT NULL,
                value_json TEXT NOT NULL,
                FOREIGN KEY (reservation_id)
                    REFERENCES reservations(id) ON DELETE CASCADE,
                FOREIGN KEY (custom_field_id)
                    REFERENCES custom_fields(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                reservation_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id) ON DELETE CASCADE
            );
            """
        )

        migrate_instrument_managers_schema(conn)

        columns = table_columns(conn, "reservations")

        if "start_date" not in columns:
            conn.execute("ALTER TABLE reservations ADD COLUMN start_date TEXT")
        if "end_date" not in columns:
            conn.execute("ALTER TABLE reservations ADD COLUMN end_date TEXT")
        if "remarks" not in columns:
            conn.execute(
                "ALTER TABLE reservations ADD COLUMN remarks TEXT NOT NULL DEFAULT ''"
            )

        columns = table_columns(conn, "reservations")
        if "reservation_date" in columns:
            conn.execute(
                """
                UPDATE reservations
                SET start_date = reservation_date
                WHERE start_date IS NULL OR start_date = ''
                """
            )
            conn.execute(
                """
                UPDATE reservations
                SET end_date = reservation_date
                WHERE end_date IS NULL OR end_date = ''
                """
            )

        admin_row = conn.execute(
            """
            SELECT id
            FROM system_admin_credentials
            WHERE id = 1
            """
        ).fetchone()

        if admin_row is None:
            salt, password_hash = make_hash(SYSTEM_ADMIN_PASSWORD)
            conn.execute(
                """
                INSERT INTO system_admin_credentials (
                    id,
                    username,
                    password_salt,
                    password_hash
                )
                VALUES (1, ?, ?, ?)
                """,
                (
                    SYSTEM_ADMIN_USERNAME,
                    salt,
                    password_hash,
                ),
            )


# ============================================================
# Security
# ============================================================

def make_hash(value: str, salt_hex: str | None = None) -> tuple[str, str]:
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        200_000,
    ).hex()

    return salt_hex, digest


def verify_hash(value: str, salt_hex: str, stored_hash: str) -> bool:
    _, calculated = make_hash(value, salt_hex)
    return hmac.compare_digest(calculated, stored_hash)


# ============================================================
# Session / browser profile
# ============================================================

def init_session() -> None:
    defaults = {
        "logged_in": False,
        "role": None,
        "manager_id": None,
        "username": None,
        "display_name": None,
        "browser_profile_loaded": False,
        "screen_width": 1200,
        "saved_name": "",
        "saved_affiliation": "",
        "saved_instrument_id": None,
        "saved_instrument_order": [],
        "access_authorized": False,
        "access_state_loaded": False,
        "pending_browser_action": None,
        "pending_scroll_top": False,
        "scroll_top_token": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def load_browser_profile() -> bool:
    if st.session_state["browser_profile_loaded"]:
        return True

    value = streamlit_js_eval(
        js_expressions="""
        JSON.stringify({
            screen_width: window.innerWidth,
            user_name: localStorage.getItem("equipment_booking_name") || "",
            affiliation: localStorage.getItem("equipment_booking_affiliation") || "",
            instrument_id: localStorage.getItem("equipment_booking_last_instrument") || "",
            instrument_order: localStorage.getItem("equipment_booking_instrument_order") || "[]",
            access_authorized: localStorage.getItem("equipment_booking_access_authorized") || ""
        })
        """,
        key="browser_profile_loader",
    )

    if value is None:
        return False

    try:
        profile = json.loads(value) if isinstance(value, str) else value
        screen_width = int(profile.get("screen_width", 1200))
        saved_name = str(profile.get("user_name", "") or "")
        saved_affiliation = str(profile.get("affiliation", "") or "")
        try:
            saved_instrument_id = int(profile.get("instrument_id", ""))
        except (TypeError, ValueError):
            saved_instrument_id = None

        order_value = profile.get("instrument_order", "[]")

        if isinstance(order_value, str):
            parsed_order = json.loads(order_value)
        else:
            parsed_order = order_value

        if not isinstance(parsed_order, list):
            parsed_order = []

        saved_instrument_order = []
        access_authorized = (
            str(profile.get("access_authorized", "")) == "authorized"
        )

        for item in parsed_order:
            try:
                instrument_id = int(item)
            except (TypeError, ValueError):
                continue

            if instrument_id not in saved_instrument_order:
                saved_instrument_order.append(instrument_id)

    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        screen_width = 1200
        saved_name = ""
        saved_affiliation = ""
        saved_instrument_id = None
        saved_instrument_order = []
        access_authorized = False

    st.session_state["screen_width"] = screen_width
    st.session_state["saved_name"] = saved_name
    st.session_state["saved_affiliation"] = saved_affiliation
    st.session_state["saved_instrument_id"] = saved_instrument_id
    st.session_state["saved_instrument_order"] = saved_instrument_order
    st.session_state["access_authorized"] = access_authorized
    st.session_state["access_state_loaded"] = True
    st.session_state["browser_profile_loaded"] = True
    return True


def queue_browser_profile_save(
    user_name: str,
    affiliation: str,
    instrument_id: int,
) -> None:
    st.session_state["saved_name"] = user_name
    st.session_state["saved_affiliation"] = affiliation
    st.session_state["saved_instrument_id"] = instrument_id
    st.session_state["pending_browser_action"] = {
        "action": "save",
        "token": secrets.token_hex(8),
        "user_name": user_name,
        "affiliation": affiliation,
        "instrument_id": instrument_id,
    }


def queue_instrument_order_save(
    instrument_order: list[int],
) -> None:
    normalized_order: list[int] = []

    for instrument_id in instrument_order:
        try:
            normalized_id = int(instrument_id)
        except (TypeError, ValueError):
            continue

        if normalized_id not in normalized_order:
            normalized_order.append(normalized_id)

    st.session_state["saved_instrument_order"] = normalized_order

    st.session_state["pending_browser_action"] = {
        "action": "save_order",
        "token": secrets.token_hex(8),
        "instrument_order": normalized_order,
    }


def queue_instrument_order_reset() -> None:
    st.session_state["saved_instrument_order"] = []

    st.session_state["pending_browser_action"] = {
        "action": "reset_order",
        "token": secrets.token_hex(8),
    }


def queue_access_authorization_save() -> None:
    st.session_state["access_authorized"] = True
    st.session_state["pending_browser_action"] = {
        "action": "authorize_access",
        "token": secrets.token_hex(8),
    }


def queue_access_authorization_clear() -> None:
    st.session_state["access_authorized"] = False
    st.session_state["pending_browser_action"] = {
        "action": "clear_access",
        "token": secrets.token_hex(8),
    }


def queue_browser_profile_clear() -> None:
    st.session_state["saved_name"] = ""
    st.session_state["saved_affiliation"] = ""
    st.session_state["saved_instrument_id"] = None
    st.session_state.pop("new_name", None)
    st.session_state.pop("new_affiliation", None)
    st.session_state["pending_browser_action"] = {
        "action": "clear",
        "token": secrets.token_hex(8),
    }


def flush_pending_browser_action() -> bool:
    pending = st.session_state.get("pending_browser_action")
    if not pending:
        return True

    token = pending["token"]

    action = pending["action"]

    if action == "save":
        script = f"""
        localStorage.setItem("equipment_booking_name", {json.dumps(pending['user_name'])});
        localStorage.setItem("equipment_booking_affiliation", {json.dumps(pending['affiliation'])});
        localStorage.setItem("equipment_booking_last_instrument", {json.dumps(str(pending['instrument_id']))});
        "saved";
        """

    elif action == "clear":
        script = """
        localStorage.removeItem("equipment_booking_name");
        localStorage.removeItem("equipment_booking_affiliation");
        localStorage.removeItem("equipment_booking_last_instrument");
        "cleared";
        """

    elif action == "save_order":
        order_json = json.dumps(
            pending["instrument_order"],
            ensure_ascii=False,
        )

        script = f"""
        localStorage.setItem(
            "equipment_booking_instrument_order",
            {json.dumps(order_json)}
        );
        "order_saved";
        """

    elif action == "reset_order":
        script = """
        localStorage.removeItem(
            "equipment_booking_instrument_order"
        );
        "order_reset";
        """

    elif action == "authorize_access":
        script = """
        localStorage.setItem(
            "equipment_booking_access_authorized",
            "authorized"
        );
        "access_authorized";
        """

    elif action == "clear_access":
        script = """
        localStorage.removeItem(
            "equipment_booking_access_authorized"
        );
        "access_cleared";
        """

    else:
        st.session_state["pending_browser_action"] = None
        return True

    result = streamlit_js_eval(
        js_expressions=script,
        key=f"browser_action_{token}",
    )

    if result is None:
        return False

    st.session_state["pending_browser_action"] = None
    return True


def request_scroll_top() -> None:
    st.session_state["pending_scroll_top"] = True
    st.session_state["scroll_top_token"] = secrets.token_hex(8)


def flush_scroll_top() -> None:
    if not st.session_state.get("pending_scroll_top"):
        return

    token = st.session_state.get("scroll_top_token") or secrets.token_hex(8)

    streamlit_js_eval(
        js_expressions="""
        (() => {
            try {
                window.parent.scrollTo(0, 0);

                const root =
                    window.parent.document.scrollingElement
                    || window.parent.document.documentElement
                    || window.parent.document.body;

                if (root) {
                    root.scrollTop = 0;
                }

                return "scrolled";
            } catch (error) {
                window.scrollTo(0, 0);
                return "fallback";
            }
        })()
        """,
        key=f"scroll_top_{token}",
    )

    st.session_state["pending_scroll_top"] = False
    st.session_state["scroll_top_token"] = None


def is_mobile_device() -> bool:
    return st.session_state["screen_width"] < MOBILE_BREAKPOINT


# ============================================================
# App access gate
# ============================================================

def render_access_gate() -> bool:
    if st.session_state.get("access_authorized"):
        return True

    st.title(APP_TITLE)
    st.subheader("利用者認証")

    st.info(
        "このシステムの利用にはアクセスコードが必要です。"
    )

    with st.form("app_access_code_form"):
        access_code = st.text_input(
            "アクセスコード",
            type="password",
            autocomplete="off",
        )
        submitted = st.form_submit_button(
            "利用開始",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if hmac.compare_digest(
            access_code,
            APP_ACCESS_CODE,
        ):
            queue_access_authorization_save()
            st.rerun()
        else:
            st.error(
                "アクセスコードが正しくありません。"
            )

    return False


# ============================================================
# Date and time
# ============================================================

def get_jst_now() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


def ceil_to_next_slot(source_datetime: datetime) -> datetime:
    base = source_datetime.replace(second=0, microsecond=0)
    remainder = base.minute % SLOT_MINUTES

    if (
        remainder == 0
        and source_datetime.second == 0
        and source_datetime.microsecond == 0
    ):
        return base

    minutes_to_add = SLOT_MINUTES - remainder if remainder else SLOT_MINUTES
    return base + timedelta(minutes=minutes_to_add)


def generate_time_options() -> list[str]:
    current = datetime.combine(date.today(), time(0, 0))
    result: list[str] = []
    for _ in range(96):
        result.append(current.strftime("%H:%M"))
        current += timedelta(minutes=SLOT_MINUTES)
    return result


TIME_OPTIONS = generate_time_options()
CALENDAR_TIMES = TIME_OPTIONS.copy()


def to_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def minutes_to_display(value: int) -> str:
    if value >= 1440:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


def combine_datetime(target_date: date, target_time: str) -> datetime:
    hour, minute = map(int, target_time.split(":"))
    return datetime.combine(target_date, time(hour=hour, minute=minute))


def reservation_start_datetime(reservation: sqlite3.Row) -> datetime:
    return combine_datetime(
        date.fromisoformat(reservation["start_date"]),
        reservation["start_time"],
    )


def reservation_end_datetime(reservation: sqlite3.Row) -> datetime:
    return combine_datetime(
        date.fromisoformat(reservation["end_date"]),
        reservation["end_time"],
    )


def get_week_start(target_date: date) -> date:
    return target_date - timedelta(days=target_date.weekday())


def format_japanese_date(target_date: date) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    return f"{target_date.month}/{target_date.day}（{weekdays[target_date.weekday()]}）"


def format_reservation_period(reservation: sqlite3.Row) -> str:
    start_date_value = date.fromisoformat(reservation["start_date"])
    end_date_value = date.fromisoformat(reservation["end_date"])
    return (
        f"{start_date_value.strftime('%Y/%m/%d')} {reservation['start_time']} ～ "
        f"{end_date_value.strftime('%Y/%m/%d')} {reservation['end_time']}"
    )


def intervals_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    return start_a < end_b and end_a > start_b


# ============================================================
# Instruments
# ============================================================

def get_instruments(active_only: bool = True) -> list[sqlite3.Row]:
    query = "SELECT * FROM instruments"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name"

    with get_connection() as conn:
        return conn.execute(query).fetchall()


def get_instrument(instrument_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM instruments WHERE id = ?",
            (instrument_id,),
        ).fetchone()


def delete_instrument(instrument_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM instruments WHERE id = ?",
            (instrument_id,),
        )


# ============================================================
# Custom fields
# ============================================================

def get_custom_fields(instrument_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM custom_fields
            WHERE instrument_id = ? AND active = 1
            ORDER BY display_order, id
            """,
            (instrument_id,),
        ).fetchall()


def get_reservation_field_values(reservation_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM reservation_field_values
            WHERE reservation_id = ?
            ORDER BY id
            """,
            (reservation_id,),
        ).fetchall()


def get_reservation_field_value_map(reservation_id: int) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for row in get_reservation_field_values(reservation_id):
        if row["custom_field_id"] is not None:
            result[row["custom_field_id"]] = json.loads(row["value_json"])
    return result


# ============================================================
# Blocked periods
# ============================================================

def get_blocked_periods(instrument_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM blocked_periods
            WHERE instrument_id = ?
            ORDER BY reservation_date, start_time
            """,
            (instrument_id,),
        ).fetchall()


def get_blocked_periods_for_range(
    instrument_id: int,
    range_start: date,
    range_end: date,
) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM blocked_periods
            WHERE
                instrument_id = ?
                AND reservation_date >= ?
                AND reservation_date <= ?
            ORDER BY reservation_date, start_time
            """,
            (
                instrument_id,
                range_start.isoformat(),
                range_end.isoformat(),
            ),
        ).fetchall()


# ============================================================
# Reservations
# ============================================================

def get_reservation(reservation_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT r.*, i.name AS instrument_name
            FROM reservations r
            JOIN instruments i ON i.id = r.instrument_id
            WHERE r.id = ?
            """,
            (reservation_id,),
        ).fetchone()


def get_reservations(instrument_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT r.*, i.name AS instrument_name
            FROM reservations r
            JOIN instruments i ON i.id = r.instrument_id
            WHERE r.instrument_id = ?
            ORDER BY r.start_date, r.start_time
            """,
            (instrument_id,),
        ).fetchall()


def get_reservations_for_range(
    instrument_id: int,
    range_start: date,
    range_end: date,
) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT r.*, i.name AS instrument_name
            FROM reservations r
            JOIN instruments i ON i.id = r.instrument_id
            WHERE
                r.instrument_id = ?
                AND r.start_date <= ?
                AND r.end_date >= ?
            ORDER BY r.start_date, r.start_time
            """,
            (
                instrument_id,
                range_end.isoformat(),
                range_start.isoformat(),
            ),
        ).fetchall()


def reservation_has_conflict(
    instrument_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_reservation_id: int | None = None,
) -> tuple[bool, str]:
    for reservation in get_reservations(instrument_id):
        if (
            exclude_reservation_id is not None
            and reservation["id"] == exclude_reservation_id
        ):
            continue

        if intervals_overlap(
            start_dt,
            end_dt,
            reservation_start_datetime(reservation),
            reservation_end_datetime(reservation),
        ):
            return True, "指定した期間には既に予約があります。"

    for blocked in get_blocked_periods(instrument_id):
        blocked_date = date.fromisoformat(blocked["reservation_date"])
        blocked_start = combine_datetime(blocked_date, blocked["start_time"])
        blocked_end = combine_datetime(blocked_date, blocked["end_time"])

        if intervals_overlap(start_dt, end_dt, blocked_start, blocked_end):
            message = "指定した期間には使用停止時間が含まれています。"
            if blocked["reason"]:
                message += f" 理由：{blocked['reason']}"
            return True, message

    return False, ""


def save_reservation_field_values(
    conn: sqlite3.Connection,
    reservation_id: int,
    instrument_id: int,
    custom_values: dict[int, Any],
) -> None:
    conn.execute(
        "DELETE FROM reservation_field_values WHERE reservation_id = ?",
        (reservation_id,),
    )

    fields = {
        field["id"]: field
        for field in get_custom_fields(instrument_id)
    }

    for field_id, value in custom_values.items():
        field = fields.get(field_id)
        if field is None:
            continue

        conn.execute(
            """
            INSERT INTO reservation_field_values (
                reservation_id,
                custom_field_id,
                field_name_snapshot,
                field_type_snapshot,
                value_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                reservation_id,
                field_id,
                field["field_name"],
                field["field_type"],
                json.dumps(value, ensure_ascii=False),
            ),
        )


def add_reservation(
    instrument_id: int,
    user_name: str,
    affiliation: str,
    start_date: date,
    end_date: date,
    start_time: str,
    end_time: str,
    purpose: str,
    purpose_other: str,
    remarks: str,
    pin: str,
    custom_values: dict[int, Any],
) -> int:
    pin_salt, pin_hash = make_hash(pin)

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reservations (
                instrument_id,
                user_name,
                affiliation,
                reservation_date,
                start_date,
                end_date,
                start_time,
                end_time,
                purpose,
                purpose_other,
                remarks,
                pin_salt,
                pin_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id,
                user_name.strip(),
                affiliation.strip(),
                start_date.isoformat(),
                start_date.isoformat(),
                end_date.isoformat(),
                start_time,
                end_time,
                purpose,
                purpose_other.strip(),
                remarks.strip(),
                pin_salt,
                pin_hash,
            ),
        )

        reservation_id = int(cursor.lastrowid)

        save_reservation_field_values(
            conn,
            reservation_id,
            instrument_id,
            custom_values,
        )

    return reservation_id


def update_reservation(
    reservation_id: int,
    user_name: str,
    affiliation: str,
    start_date: date,
    end_date: date,
    start_time: str,
    end_time: str,
    purpose: str,
    purpose_other: str,
    remarks: str,
    custom_values: dict[int, Any],
) -> None:
    reservation = get_reservation(reservation_id)
    if reservation is None:
        return

    instrument_id = reservation["instrument_id"]

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE reservations
            SET
                user_name = ?,
                affiliation = ?,
                reservation_date = ?,
                start_date = ?,
                end_date = ?,
                start_time = ?,
                end_time = ?,
                purpose = ?,
                purpose_other = ?,
                remarks = ?
            WHERE id = ?
            """,
            (
                user_name.strip(),
                affiliation.strip(),
                start_date.isoformat(),
                start_date.isoformat(),
                end_date.isoformat(),
                start_time,
                end_time,
                purpose,
                purpose_other.strip(),
                remarks.strip(),
                reservation_id,
            ),
        )

        save_reservation_field_values(
            conn,
            reservation_id,
            instrument_id,
            custom_values,
        )


def delete_reservation(reservation_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))


# ============================================================
# Login / permissions
# ============================================================

def logout() -> None:
    st.session_state["logged_in"] = False
    st.session_state["role"] = None
    st.session_state["manager_id"] = None
    st.session_state["username"] = None
    st.session_state["display_name"] = None
    st.rerun()


def authenticate(username: str, password: str) -> bool:
    username = username.strip()

    with get_connection() as conn:
        admin_account = conn.execute(
            """
            SELECT *
            FROM system_admin_credentials
            WHERE id = 1 AND username = ?
            """,
            (username,),
        ).fetchone()

    if (
        admin_account is not None
        and verify_hash(
            password,
            admin_account["password_salt"],
            admin_account["password_hash"],
        )
    ):
        st.session_state["logged_in"] = True
        st.session_state["role"] = "system_admin"
        st.session_state["manager_id"] = None
        st.session_state["username"] = admin_account["username"]
        st.session_state["display_name"] = "システム管理者"
        return True

    with get_connection() as conn:
        account = conn.execute(
            """
            SELECT *
            FROM manager_accounts
            WHERE username = ? AND active = 1
            """,
            (username,),
        ).fetchone()

    if account is None:
        return False

    if not verify_hash(
        password,
        account["password_salt"],
        account["password_hash"],
    ):
        return False

    st.session_state["logged_in"] = True
    st.session_state["role"] = "instrument_manager"
    st.session_state["manager_id"] = account["id"]
    st.session_state["username"] = account["username"]
    st.session_state["display_name"] = account["display_name"]
    return True


def verify_current_user_password(password: str) -> bool:
    role = st.session_state.get("role")

    if role == "system_admin":
        with get_connection() as conn:
            account = conn.execute(
                """
                SELECT *
                FROM system_admin_credentials
                WHERE id = 1
                """
            ).fetchone()

    elif role == "instrument_manager":
        manager_id = st.session_state.get("manager_id")
        if manager_id is None:
            return False

        with get_connection() as conn:
            account = conn.execute(
                """
                SELECT *
                FROM manager_accounts
                WHERE id = ?
                """,
                (manager_id,),
            ).fetchone()

    else:
        return False

    if account is None:
        return False

    return verify_hash(
        password,
        account["password_salt"],
        account["password_hash"],
    )


def change_current_user_password(new_password: str) -> None:
    salt, password_hash = make_hash(new_password)
    role = st.session_state.get("role")

    with get_connection() as conn:
        if role == "system_admin":
            conn.execute(
                """
                UPDATE system_admin_credentials
                SET
                    password_salt = ?,
                    password_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (
                    salt,
                    password_hash,
                ),
            )

        elif role == "instrument_manager":
            manager_id = st.session_state.get("manager_id")
            if manager_id is None:
                raise RuntimeError("機器管理者情報を確認できません。")

            conn.execute(
                """
                UPDATE manager_accounts
                SET
                    password_salt = ?,
                    password_hash = ?
                WHERE id = ?
                """,
                (
                    salt,
                    password_hash,
                    manager_id,
                ),
            )

        else:
            raise RuntimeError("ログイン情報を確認できません。")


def render_own_password_change() -> None:
    with st.expander("パスワード変更"):
        with st.form("own_password_change_form"):
            current_password = st.text_input(
                "現在のパスワード *",
                type="password",
            )
            new_password = st.text_input(
                "新しいパスワード *",
                type="password",
            )
            new_password_confirm = st.text_input(
                "新しいパスワード（確認） *",
                type="password",
            )
            submitted = st.form_submit_button("パスワードを変更")

        if submitted:
            if not current_password:
                st.error("現在のパスワードを入力してください。")
            elif not verify_current_user_password(current_password):
                st.error("現在のパスワードが正しくありません。")
            elif len(new_password) < 8:
                st.error("新しいパスワードは8文字以上にしてください。")
            elif new_password != new_password_confirm:
                st.error("新しいパスワードが一致しません。")
            elif new_password == current_password:
                st.error("現在とは異なるパスワードを設定してください。")
            else:
                change_current_user_password(new_password)
                st.success("パスワードを変更しました。")


def get_managed_instrument_ids(manager_id: int) -> list[int]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT instrument_id
            FROM instrument_managers
            WHERE manager_id = ?
            """,
            (manager_id,),
        ).fetchall()

    return [row["instrument_id"] for row in rows]


def manageable_instrument_ids() -> list[int]:
    if st.session_state["role"] == "system_admin":
        return [
            instrument["id"]
            for instrument in get_instruments(active_only=False)
        ]

    manager_id = st.session_state["manager_id"]
    if manager_id is None:
        return []

    return get_managed_instrument_ids(manager_id)


# ============================================================
# Display helpers
# ============================================================

def purpose_label(reservation: sqlite3.Row) -> str:
    if reservation["purpose"] == "その他":
        detail = reservation["purpose_other"].strip()
        return f"その他（{detail}）" if detail else "その他"
    return reservation["purpose"]


def field_value_is_empty(field: sqlite3.Row, value: Any) -> bool:
    if field["field_type"] == "checkbox":
        return value is False
    if field["field_type"] == "multiselect":
        return len(value) == 0
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def render_custom_field(
    field: sqlite3.Row,
    key_prefix: str,
    default_value: Any = None,
) -> Any:
    label = field["field_name"] + (" *" if field["required"] else "")
    widget_key = f"{key_prefix}_{field['id']}"
    field_type = field["field_type"]
    options = json.loads(field["options_json"] or "[]")

    if field_type == "text":
        return st.text_input(
            label,
            value="" if default_value is None else str(default_value),
            key=widget_key,
        )

    if field_type == "textarea":
        return st.text_area(
            label,
            value="" if default_value is None else str(default_value),
            key=widget_key,
        )

    if field_type == "select":
        select_options = ["選択してください"] + options
        index = options.index(default_value) + 1 if default_value in options else 0
        selected = st.selectbox(
            label,
            select_options,
            index=index,
            key=widget_key,
        )
        return "" if selected == "選択してください" else selected

    if field_type == "multiselect":
        defaults = default_value if isinstance(default_value, list) else []
        return st.multiselect(
            label,
            options,
            default=[item for item in defaults if item in options],
            key=widget_key,
        )

    if field_type == "number":
        try:
            number_value = float(default_value)
        except (TypeError, ValueError):
            number_value = 0.0
        return st.number_input(
            label,
            min_value=0.0,
            value=number_value,
            step=1.0,
            key=widget_key,
        )

    if field_type == "checkbox":
        return st.checkbox(
            label,
            value=bool(default_value),
            key=widget_key,
        )

    return ""


def display_custom_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(map(str, value))
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    return str(value)


# ============================================================
# Calendar
# ============================================================

def reservation_segment_for_date(
    reservation: sqlite3.Row,
    target_date: date,
) -> tuple[int, int] | None:
    reservation_start = reservation_start_datetime(reservation)
    reservation_end = reservation_end_datetime(reservation)
    day_start = datetime.combine(target_date, time(0, 0))
    day_end = day_start + timedelta(days=1)

    segment_start = max(reservation_start, day_start)
    segment_end = min(reservation_end, day_end)

    if segment_start >= segment_end:
        return None

    start_minutes = int((segment_start - day_start).total_seconds() // 60)
    end_minutes = int((segment_end - day_start).total_seconds() // 60)
    return start_minutes, end_minutes


def build_calendar_html(
    dates: list[date],
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
    instrument_id: int,
    compact_mode: bool,
) -> str:
    column_count = len(dates)
    slot_count = 96
    day_width = 125 if compact_mode else 165
    minimum_width = 68 + column_count * day_width
    now = get_jst_now()

    css = f"""
    <style>
    html, body {{
        margin: 0;
        padding: 0;
        background: transparent;
    }}
    .calendar-scroll {{
        width: 100%;
        overflow-x: auto;
        overflow-y: hidden;
        -webkit-overflow-scrolling: touch;
        border: 1px solid #d9d9d9;
        border-radius: 8px;
        background: white;
    }}
    .calendar {{
        min-width: {minimum_width}px;
        display: grid;
        grid-template-columns:
            68px repeat({column_count}, minmax({day_width}px, 1fr));
        grid-template-rows:
            48px repeat({slot_count}, {SLOT_HEIGHT}px);
        position: relative;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 13px;
    }}
    .header {{
        position: sticky;
        top: 0;
        z-index: 10;
        padding: 10px 3px;
        text-align: center;
        font-weight: 600;
        border-right: 1px solid #ddd;
        border-bottom: 1px solid #bbb;
        background: #f7f7f7;
        box-sizing: border-box;
    }}
    .time {{
        padding: 3px 5px;
        text-align: right;
        color: #666;
        font-size: 11px;
        border-right: 1px solid #ddd;
        border-bottom: 1px solid #eee;
        background: #fafafa;
        box-sizing: border-box;
    }}
    .cell, .cell-link {{
        border-right: 1px solid #e0e0e0;
        border-bottom: 1px solid #eeeeee;
        background: white;
        box-sizing: border-box;
    }}
    .cell-link {{
        display: block;
        position: relative;
        text-decoration: none;
        color: inherit;
        z-index: 1;
        -webkit-tap-highlight-color: rgba(37, 99, 235, 0.16);
    }}
    .cell-link:hover {{
        background: #eff6ff;
        cursor: pointer;
    }}
    .cell-link:hover::after {{
        content: "＋";
        position: absolute;
        top: 1px;
        right: 4px;
        font-size: 12px;
        color: #2563eb;
    }}
    .hour {{border-top: 1px solid #bdbdbd;}}
    .reservation-link {{
        z-index: 5;
        margin: 2px;
        text-decoration: none;
        color: inherit;
        display: block;
        overflow: hidden;
        border-radius: 4px;
        -webkit-tap-highlight-color: rgba(37, 99, 235, 0.2);
    }}
    .reservation {{
        width: 100%;
        height: 100%;
        padding: 4px 5px;
        border-radius: 4px;
        background: #dbeafe;
        border-left: 4px solid #2563eb;
        color: #1f2937;
        box-sizing: border-box;
        overflow: hidden;
        line-height: 1.2;
    }}
    .reservation:hover, .reservation:active {{
        background: #bfdbfe;
        box-shadow: 0 1px 5px rgba(0, 0, 0, 0.18);
        cursor: pointer;
    }}
    .blocked {{
        z-index: 6;
        margin: 2px;
        padding: 4px 5px;
        border-radius: 4px;
        background: #f3f4f6;
        border-left: 4px solid #6b7280;
        color: #374151;
        box-sizing: border-box;
        overflow: hidden;
        line-height: 1.2;
    }}
    .name {{font-weight: 600; font-size: 12px;}}
    .small {{font-size: 10px;}}
    </style>
    """

    content = [
        css,
        '<div class="calendar-scroll">',
        '<div class="calendar">',
        '<div class="header" style="grid-column:1;grid-row:1;">時刻</div>',
    ]

    for day_index, target_date in enumerate(dates):
        grid_column = day_index + 2
        label = html.escape(format_japanese_date(target_date))
        content.append(
            f'<div class="header" style="grid-column:{grid_column};grid-row:1;">'
            f'{label}</div>'
        )

    for slot_index, slot_time in enumerate(CALENDAR_TIMES):
        grid_row = slot_index + 2
        minute = int(slot_time.split(":")[1])
        hour_class = " hour" if minute == 0 else ""
        time_label = slot_time if minute in {0, 30} else ""

        content.append(
            f'<div class="time{hour_class}" '
            f'style="grid-column:1;grid-row:{grid_row};">{time_label}</div>'
        )

        for day_index, target_date in enumerate(dates):
            grid_column = day_index + 2
            slot_datetime = combine_datetime(target_date, slot_time)

            if slot_datetime >= now:
                href = (
                    "?view=new"
                    f"&instrument_id={instrument_id}"
                    f"&start_date={target_date.isoformat()}"
                    f"&start_time={slot_time}"
                )
                content.append(
                    f'<a class="cell-link{hour_class}" href="{href}" target="_self" '
                    f'title="この時刻から予約" '
                    f'style="grid-column:{grid_column};grid-row:{grid_row};"></a>'
                )
            else:
                content.append(
                    f'<div class="cell{hour_class}" '
                    f'style="grid-column:{grid_column};grid-row:{grid_row};"></div>'
                )

    for reservation in reservations:
        for day_index, target_date in enumerate(dates):
            segment = reservation_segment_for_date(reservation, target_date)
            if segment is None:
                continue

            start_minutes, end_minutes = segment
            start_slot = start_minutes // SLOT_MINUTES
            end_slot = end_minutes // SLOT_MINUTES
            span = max(1, end_slot - start_slot)
            grid_column = day_index + 2
            grid_row = start_slot + 2

            name = html.escape(reservation["user_name"])
            purpose = html.escape(purpose_label(reservation))
            segment_time_text = html.escape(
                f"{minutes_to_display(start_minutes)}–"
                f"{minutes_to_display(end_minutes)}"
            )

            href = (
                "?view=reservation"
                f"&reservation_id={reservation['id']}"
                f"&instrument_id={instrument_id}"
            )

            purpose_html = (
                ""
                if compact_mode
                else f'<div class="small">{purpose}</div>'
            )

            content.append(
                f'<a class="reservation-link" href="{href}" target="_self" '
                f'style="grid-column:{grid_column};'
                f'grid-row:{grid_row} / span {span};">'
                f'<div class="reservation">'
                f'<div class="name">{name}</div>'
                f'{purpose_html}'
                f'<div class="small">{segment_time_text}</div>'
                f'</div></a>'
            )

    for blocked in blocked_periods:
        blocked_date = date.fromisoformat(blocked["reservation_date"])
        if blocked_date not in dates:
            continue

        day_index = dates.index(blocked_date)
        start_slot = to_minutes(blocked["start_time"]) // SLOT_MINUTES
        end_slot = to_minutes(blocked["end_time"]) // SLOT_MINUTES
        span = max(1, end_slot - start_slot)
        grid_column = day_index + 2
        grid_row = start_slot + 2
        reason = html.escape(blocked["reason"] or "使用停止")

        content.append(
            f'<div class="blocked" '
            f'style="grid-column:{grid_column};'
            f'grid-row:{grid_row} / span {span};">'
            f'<div class="name">使用停止</div>'
            f'<div class="small">{reason}</div>'
            f'</div>'
        )

    content.append("</div></div>")
    return "".join(content)


def render_calendar(
    dates: list[date],
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
    instrument_id: int,
    compact_mode: bool,
) -> None:
    calendar_html = build_calendar_html(
        dates,
        reservations,
        blocked_periods,
        instrument_id,
        compact_mode,
    )

    # カレンダーはiframeではなくメイン画面へ直接描画する。
    # リンクは現在のブラウザタブ内で遷移するよう明示する。
    # サイドバーを含むアプリの入れ子表示や新規タブ生成を防ぐ。
    st.markdown(
        calendar_html,
        unsafe_allow_html=True,
    )


# ============================================================
# Navigation
# ============================================================

def reset_new_reservation_state() -> None:
    for key in [
        "new_start_date",
        "new_end_date",
        "new_start_time",
        "new_end_time",
        "new_purpose",
        "new_purpose_other",
        "new_remarks",
        "new_pin",
        "new_source_token",
    ]:
        st.session_state.pop(key, None)

    for key in [
        key
        for key in list(st.session_state.keys())
        if key.startswith("new_field_")
    ]:
        st.session_state.pop(key, None)


def open_booking_view(instrument_id: int) -> None:
    st.query_params.clear()
    st.query_params["instrument_id"] = str(instrument_id)
    st.rerun()


def open_new_reservation_view(instrument_id: int) -> None:
    reset_new_reservation_state()
    st.query_params.clear()
    st.query_params["view"] = "new"
    st.query_params["instrument_id"] = str(instrument_id)
    st.rerun()


def open_reservation_detail_view(
    instrument_id: int,
    reservation_id: int,
) -> None:
    st.query_params.clear()
    st.query_params["view"] = "reservation"
    st.query_params["instrument_id"] = str(instrument_id)
    st.query_params["reservation_id"] = str(reservation_id)
    st.rerun()


def open_edit_reservation_view(
    instrument_id: int,
    reservation_id: int,
) -> None:
    st.query_params.clear()
    st.query_params["view"] = "edit"
    st.query_params["instrument_id"] = str(instrument_id)
    st.query_params["reservation_id"] = str(reservation_id)
    st.rerun()


# ============================================================
# New reservation
# ============================================================

def initialize_new_reservation_defaults(instrument_id: int) -> None:
    clicked_date_text = st.query_params.get("start_date", "")
    clicked_time = st.query_params.get("start_time", "")

    valid_clicked_datetime: datetime | None = None

    if clicked_date_text and clicked_time in TIME_OPTIONS:
        try:
            clicked_date = date.fromisoformat(clicked_date_text)
            clicked_datetime = combine_datetime(clicked_date, clicked_time)
            if clicked_datetime >= get_jst_now():
                valid_clicked_datetime = clicked_datetime
        except ValueError:
            pass

    if valid_clicked_datetime is not None:
        start_datetime = valid_clicked_datetime
        source_token = f"calendar_{instrument_id}_{start_datetime.isoformat()}"
    else:
        start_datetime = ceil_to_next_slot(get_jst_now())
        source_token = f"default_{instrument_id}"

    if st.session_state.get("new_source_token") != source_token:
        default_end = start_datetime + timedelta(hours=1)
        st.session_state["new_start_date"] = start_datetime.date()
        st.session_state["new_start_time"] = start_datetime.strftime("%H:%M")
        st.session_state["new_end_date"] = default_end.date()
        st.session_state["new_end_time"] = default_end.strftime("%H:%M")
        st.session_state["new_source_token"] = source_token

    if "new_name" not in st.session_state:
        st.session_state["new_name"] = st.session_state["saved_name"]

    if "new_affiliation" not in st.session_state:
        st.session_state["new_affiliation"] = st.session_state["saved_affiliation"]


def sync_new_end_from_start() -> None:
    start_dt = combine_datetime(
        st.session_state["new_start_date"],
        st.session_state["new_start_time"],
    )
    default_end = start_dt + timedelta(hours=1)
    st.session_state["new_end_date"] = default_end.date()
    st.session_state["new_end_time"] = default_end.strftime("%H:%M")


def render_new_reservation_page(instrument_id: int) -> None:
    instrument = get_instrument(instrument_id)
    if instrument is None:
        st.error("機器が見つかりません。")
        return

    initialize_new_reservation_defaults(instrument_id)
    mobile = is_mobile_device()

    st.header(instrument["name"])
    st.subheader("新規予約")

    if st.button("← 予約状況に戻る", use_container_width=mobile):
        open_booking_view(instrument_id)

    if instrument["description"]:
        st.caption(instrument["description"])

    if instrument["notice"]:
        st.info(instrument["notice"])

    user_name = st.text_input("氏名 *", key="new_name")
    affiliation = st.text_input("所属講座 *", key="new_affiliation")

    if mobile:
        start_date_value = st.date_input(
            "開始日 *",
            min_value=get_jst_now().date(),
            key="new_start_date",
            format="YYYY/MM/DD",
            on_change=sync_new_end_from_start,
        )
        start_time_value = st.selectbox(
            "開始時刻 *",
            TIME_OPTIONS,
            key="new_start_time",
            on_change=sync_new_end_from_start,
        )
        end_date_value = st.date_input(
            "終了日 *",
            min_value=get_jst_now().date(),
            key="new_end_date",
            format="YYYY/MM/DD",
        )
        end_time_value = st.selectbox(
            "終了時刻 *",
            TIME_OPTIONS,
            key="new_end_time",
        )
    else:
        col1, col2 = st.columns(2)

        with col1:
            start_date_value = st.date_input(
                "開始日 *",
                min_value=get_jst_now().date(),
                key="new_start_date",
                format="YYYY/MM/DD",
                on_change=sync_new_end_from_start,
            )
        with col2:
            end_date_value = st.date_input(
                "終了日 *",
                min_value=get_jst_now().date(),
                key="new_end_date",
                format="YYYY/MM/DD",
            )

        col1, col2 = st.columns(2)

        with col1:
            start_time_value = st.selectbox(
                "開始時刻 *",
                TIME_OPTIONS,
                key="new_start_time",
                on_change=sync_new_end_from_start,
            )
        with col2:
            end_time_value = st.selectbox(
                "終了時刻 *",
                TIME_OPTIONS,
                key="new_end_time",
            )

    purpose = st.selectbox(
        "使用目的 *",
        ["測定", "解析のみ", "その他"],
        key="new_purpose",
    )

    purpose_other = ""
    if purpose == "その他":
        purpose_other = st.text_input(
            "「その他」の内容 *",
            key="new_purpose_other",
        )

    remarks = st.text_area("備考", key="new_remarks")

    fields = get_custom_fields(instrument_id)
    custom_values: dict[int, Any] = {}

    if fields:
        st.markdown("#### 機器固有の入力項目")
        for field in fields:
            custom_values[field["id"]] = render_custom_field(
                field,
                "new_field",
            )

    pin = st.text_input(
        "4桁の暗証番号 *",
        type="password",
        max_chars=4,
        key="new_pin",
        help="予約の編集・取消時に必要です。",
    )

    if st.button("予約する", type="primary", use_container_width=True):
        errors: list[str] = []

        start_dt = combine_datetime(start_date_value, start_time_value)
        end_dt = combine_datetime(end_date_value, end_time_value)
        now = get_jst_now()

        if not user_name.strip():
            errors.append("氏名を入力してください。")
        if not affiliation.strip():
            errors.append("所属講座を入力してください。")
        if start_dt < now:
            errors.append("過去の日時から予約を開始することはできません。")
        if end_dt <= start_dt:
            errors.append("終了日時は開始日時より後に設定してください。")
        if purpose == "その他" and not purpose_other.strip():
            errors.append("「その他」の内容を入力してください。")
        if not (pin.isdigit() and len(pin) == 4):
            errors.append("暗証番号は4桁の数字で入力してください。")

        for field in fields:
            value = custom_values[field["id"]]
            if field["required"] and field_value_is_empty(field, value):
                errors.append(f"「{field['field_name']}」を入力してください。")

        if errors:
            for error in errors:
                st.error(error)
            return

        conflict, message = reservation_has_conflict(
            instrument_id,
            start_dt,
            end_dt,
        )

        if conflict:
            st.error(message)
            return

        reservation_id = add_reservation(
            instrument_id=instrument_id,
            user_name=user_name,
            affiliation=affiliation,
            start_date=start_date_value,
            end_date=end_date_value,
            start_time=start_time_value,
            end_time=end_time_value,
            purpose=purpose,
            purpose_other=purpose_other,
            remarks=remarks,
            pin=pin,
            custom_values=custom_values,
        )

        queue_browser_profile_save(
            user_name=user_name.strip(),
            affiliation=affiliation.strip(),
            instrument_id=instrument_id,
        )

        reset_new_reservation_state()
        open_reservation_detail_view(instrument_id, reservation_id)


# ============================================================
# Reservation detail
# ============================================================

def process_reservation_delete(
    reservation: sqlite3.Row,
    pin: str,
    instrument_id: int,
) -> None:
    if not verify_hash(
        pin,
        reservation["pin_salt"],
        reservation["pin_hash"],
    ):
        st.error("暗証番号が正しくありません。")
        return

    if reservation_end_datetime(reservation) <= get_jst_now():
        st.error("終了済みの予約は取り消せません。")
        return

    delete_reservation(reservation["id"])
    open_booking_view(instrument_id)


def render_reservation_detail_page(
    instrument_id: int,
    reservation_id: int,
) -> None:
    reservation = get_reservation(reservation_id)

    if reservation is None:
        st.error("予約が見つかりません。")
        return

    if reservation["instrument_id"] != instrument_id:
        st.error("予約情報と機器情報が一致しません。")
        return

    mobile = is_mobile_device()

    st.header(reservation["instrument_name"])
    st.subheader("予約詳細")

    if st.button("← 予約状況に戻る", use_container_width=mobile):
        open_booking_view(instrument_id)

    with st.container(border=True):
        st.write(f"**予約者：** {reservation['user_name']}")
        st.write(f"**所属講座：** {reservation['affiliation']}")
        st.write(f"**予約期間：** {format_reservation_period(reservation)}")
        st.write(f"**使用目的：** {purpose_label(reservation)}")

        if reservation["remarks"]:
            st.write(f"**備考：** {reservation['remarks']}")

        for field_value in get_reservation_field_values(reservation_id):
            value = json.loads(field_value["value_json"])
            st.write(
                f"**{field_value['field_name_snapshot']}：** "
                f"{display_custom_value(value)}"
            )

    if reservation_end_datetime(reservation) <= get_jst_now():
        st.info("この予約は既に終了しているため、編集・取消はできません。")
        return

    st.markdown("### 予約の編集・取消")

    pin = st.text_input(
        "4桁の暗証番号",
        type="password",
        max_chars=4,
        key=f"detail_pin_{reservation_id}",
    )

    def handle_edit() -> None:
        if not verify_hash(
            pin,
            reservation["pin_salt"],
            reservation["pin_hash"],
        ):
            st.error("暗証番号が正しくありません。")
        else:
            open_edit_reservation_view(instrument_id, reservation_id)

    if mobile:
        if st.button(
            "予約を編集",
            type="primary",
            use_container_width=True,
            key=f"detail_edit_{reservation_id}",
        ):
            handle_edit()

        if st.button(
            "予約を取り消す",
            use_container_width=True,
            key=f"detail_delete_{reservation_id}",
        ):
            process_reservation_delete(reservation, pin, instrument_id)
    else:
        col1, col2 = st.columns(2)

        with col1:
            if st.button(
                "予約を編集",
                type="primary",
                use_container_width=True,
                key=f"detail_edit_{reservation_id}",
            ):
                handle_edit()

        with col2:
            if st.button(
                "予約を取り消す",
                use_container_width=True,
                key=f"detail_delete_{reservation_id}",
            ):
                process_reservation_delete(reservation, pin, instrument_id)


# ============================================================
# Edit reservation
# ============================================================

def render_edit_reservation_page(
    instrument_id: int,
    reservation_id: int,
) -> None:
    reservation = get_reservation(reservation_id)

    if reservation is None:
        st.error("予約が見つかりません。")
        return

    if reservation["instrument_id"] != instrument_id:
        st.error("予約情報と機器情報が一致しません。")
        return

    if reservation_end_datetime(reservation) <= get_jst_now():
        st.error("この予約は既に終了しているため、編集できません。")
        return

    mobile = is_mobile_device()

    st.header(reservation["instrument_name"])
    st.subheader("予約編集")

    if st.button("← 予約詳細に戻る", use_container_width=mobile):
        open_reservation_detail_view(instrument_id, reservation_id)

    field_values = get_reservation_field_value_map(reservation_id)
    fields = get_custom_fields(instrument_id)

    user_name = st.text_input(
        "氏名 *",
        value=reservation["user_name"],
        key=f"edit_name_{reservation_id}",
    )

    affiliation = st.text_input(
        "所属講座 *",
        value=reservation["affiliation"],
        key=f"edit_affiliation_{reservation_id}",
    )

    start_date_initial = date.fromisoformat(reservation["start_date"])
    end_date_initial = date.fromisoformat(reservation["end_date"])

    if mobile:
        start_date_value = st.date_input(
            "開始日 *",
            value=start_date_initial,
            key=f"edit_start_date_{reservation_id}",
            format="YYYY/MM/DD",
        )
        start_time_value = st.selectbox(
            "開始時刻 *",
            TIME_OPTIONS,
            index=TIME_OPTIONS.index(reservation["start_time"]),
            key=f"edit_start_time_{reservation_id}",
        )
        end_date_value = st.date_input(
            "終了日 *",
            value=end_date_initial,
            key=f"edit_end_date_{reservation_id}",
            format="YYYY/MM/DD",
        )
        end_time_value = st.selectbox(
            "終了時刻 *",
            TIME_OPTIONS,
            index=TIME_OPTIONS.index(reservation["end_time"]),
            key=f"edit_end_time_{reservation_id}",
        )
    else:
        col1, col2 = st.columns(2)

        with col1:
            start_date_value = st.date_input(
                "開始日 *",
                value=start_date_initial,
                key=f"edit_start_date_{reservation_id}",
                format="YYYY/MM/DD",
            )
        with col2:
            end_date_value = st.date_input(
                "終了日 *",
                value=end_date_initial,
                key=f"edit_end_date_{reservation_id}",
                format="YYYY/MM/DD",
            )

        col1, col2 = st.columns(2)

        with col1:
            start_time_value = st.selectbox(
                "開始時刻 *",
                TIME_OPTIONS,
                index=TIME_OPTIONS.index(reservation["start_time"]),
                key=f"edit_start_time_{reservation_id}",
            )
        with col2:
            end_time_value = st.selectbox(
                "終了時刻 *",
                TIME_OPTIONS,
                index=TIME_OPTIONS.index(reservation["end_time"]),
                key=f"edit_end_time_{reservation_id}",
            )

    purpose_options = ["測定", "解析のみ", "その他"]

    purpose = st.selectbox(
        "使用目的 *",
        purpose_options,
        index=purpose_options.index(reservation["purpose"]),
        key=f"edit_purpose_{reservation_id}",
    )

    purpose_other = reservation["purpose_other"]

    if purpose == "その他":
        purpose_other = st.text_input(
            "「その他」の内容 *",
            value=reservation["purpose_other"],
            key=f"edit_other_{reservation_id}",
        )

    remarks = st.text_area(
        "備考",
        value=reservation["remarks"],
        key=f"edit_remarks_{reservation_id}",
    )

    custom_values: dict[int, Any] = {}

    if fields:
        st.markdown("#### 機器固有の入力項目")
        for field in fields:
            custom_values[field["id"]] = render_custom_field(
                field,
                f"edit_field_{reservation_id}",
                field_values.get(field["id"]),
            )

    if st.button("変更を保存", type="primary", use_container_width=True):
        errors: list[str] = []

        start_dt = combine_datetime(start_date_value, start_time_value)
        end_dt = combine_datetime(end_date_value, end_time_value)

        if not user_name.strip():
            errors.append("氏名を入力してください。")
        if not affiliation.strip():
            errors.append("所属講座を入力してください。")
        if end_dt <= start_dt:
            errors.append("終了日時は開始日時より後に設定してください。")
        if end_dt <= get_jst_now():
            errors.append("終了済みの日時へ変更することはできません。")
        if purpose == "その他" and not purpose_other.strip():
            errors.append("「その他」の内容を入力してください。")

        for field in fields:
            value = custom_values[field["id"]]
            if field["required"] and field_value_is_empty(field, value):
                errors.append(f"「{field['field_name']}」を入力してください。")

        if errors:
            for error in errors:
                st.error(error)
            return

        conflict, message = reservation_has_conflict(
            instrument_id,
            start_dt,
            end_dt,
            exclude_reservation_id=reservation_id,
        )

        if conflict:
            st.error(message)
            return

        update_reservation(
            reservation_id=reservation_id,
            user_name=user_name,
            affiliation=affiliation,
            start_date=start_date_value,
            end_date=end_date_value,
            start_time=start_time_value,
            end_time=end_time_value,
            purpose=purpose,
            purpose_other=purpose_other,
            remarks=remarks,
            custom_values=custom_values,
        )

        queue_browser_profile_save(
            user_name=user_name.strip(),
            affiliation=affiliation.strip(),
            instrument_id=instrument_id,
        )

        open_reservation_detail_view(instrument_id, reservation_id)


# ============================================================
# Instrument display order
# ============================================================

def normalize_instrument_order(
    instruments: list[sqlite3.Row],
    saved_order: list[int] | None = None,
) -> list[int]:
    available_ids = [
        instrument["id"]
        for instrument in instruments
    ]

    normalized: list[int] = []

    for instrument_id in (
        saved_order
        if saved_order is not None
        else st.session_state.get(
            "saved_instrument_order",
            [],
        )
    ):
        if (
            instrument_id in available_ids
            and instrument_id not in normalized
        ):
            normalized.append(
                instrument_id
            )

    for instrument_id in available_ids:
        if instrument_id not in normalized:
            normalized.append(
                instrument_id
            )

    return normalized


def sort_instruments_for_browser(
    instruments: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    order = normalize_instrument_order(
        instruments
    )

    order_index = {
        instrument_id: index
        for index, instrument_id in enumerate(
            order
        )
    }

    return sorted(
        instruments,
        key=lambda instrument: order_index[
            instrument["id"]
        ],
    )


def render_instrument_order_settings(
    instruments: list[sqlite3.Row],
) -> None:
    default_order = [
        instrument["id"]
        for instrument in instruments
    ]

    normalized_saved_order = (
        normalize_instrument_order(
            instruments
        )
    )

    draft_key = "instrument_order_draft"

    draft_order = st.session_state.get(
        draft_key
    )

    if (
        not isinstance(
            draft_order,
            list,
        )
        or set(draft_order)
        != set(default_order)
        or len(draft_order)
        != len(default_order)
    ):
        st.session_state[
            draft_key
        ] = normalized_saved_order.copy()

    with st.expander(
        "機器表示順の設定"
    ):
        st.caption(
            "このブラウザでの機器表示順を変更できます。"
        )

        instrument_by_id = {
            instrument["id"]: instrument
            for instrument in instruments
        }

        current_order = st.session_state[
            draft_key
        ].copy()

        for index, instrument_id in enumerate(
            current_order
        ):
            instrument = instrument_by_id[
                instrument_id
            ]

            col_name, col_up, col_down = st.columns(
                [6, 1, 1]
            )

            with col_name:
                st.write(
                    instrument["name"]
                )

            with col_up:
                if st.button(
                    "↑",
                    disabled=index == 0,
                    key=(
                        "instrument_order_up_"
                        f"{instrument_id}"
                    ),
                    help="上へ移動",
                    use_container_width=True,
                ):
                    current_order[
                        index - 1
                    ], current_order[
                        index
                    ] = (
                        current_order[index],
                        current_order[index - 1],
                    )

                    st.session_state[
                        draft_key
                    ] = current_order

                    st.rerun()

            with col_down:
                if st.button(
                    "↓",
                    disabled=(
                        index
                        == len(
                            current_order
                        ) - 1
                    ),
                    key=(
                        "instrument_order_down_"
                        f"{instrument_id}"
                    ),
                    help="下へ移動",
                    use_container_width=True,
                ):
                    current_order[
                        index + 1
                    ], current_order[
                        index
                    ] = (
                        current_order[index],
                        current_order[index + 1],
                    )

                    st.session_state[
                        draft_key
                    ] = current_order

                    st.rerun()

        save_col, reset_col = st.columns(2)

        with save_col:
            if st.button(
                "表示順を保存",
                type="primary",
                use_container_width=True,
                key="save_instrument_order",
            ):
                queue_instrument_order_save(
                    st.session_state[
                        draft_key
                    ]
                )

                st.rerun()

        with reset_col:
            if st.button(
                "初期順に戻す",
                use_container_width=True,
                key="reset_instrument_order",
            ):
                st.session_state[
                    draft_key
                ] = default_order.copy()

                queue_instrument_order_reset()

                st.rerun()


# ============================================================
# Booking page
# ============================================================

def render_booking_page(
    instrument_id: int,
) -> None:
    instrument = get_instrument(instrument_id)

    if instrument is None:
        st.error("機器が見つかりません。")
        return

    mobile = is_mobile_device()

    st.header(instrument["name"])

    if instrument["notice"]:
        st.info(instrument["notice"])

    if st.button(
        "＋ 新規予約",
        type="primary",
        use_container_width=mobile,
        key=f"new_reservation_button_{instrument_id}",
    ):
        open_new_reservation_view(instrument_id)

    default_view_index = 1 if mobile else 0

    if mobile:
        view_mode = st.radio(
            "表示",
            ["週間", "1日"],
            index=default_view_index,
            horizontal=True,
            key="booking_view_mode",
        )
        selected_date = st.date_input(
            "表示日",
            value=get_jst_now().date(),
            format="YYYY/MM/DD",
            key="booking_display_date",
        )
    else:
        col1, col2 = st.columns(2)

        with col1:
            view_mode = st.radio(
                "表示",
                ["週間", "1日"],
                index=default_view_index,
                horizontal=True,
                key="booking_view_mode",
            )
        with col2:
            selected_date = st.date_input(
                "表示日",
                value=get_jst_now().date(),
                format="YYYY/MM/DD",
                key="booking_display_date",
            )

    if view_mode == "週間":
        start_date_value = get_week_start(selected_date)
        end_date_value = start_date_value + timedelta(days=6)
        dates = [
            start_date_value + timedelta(days=index)
            for index in range(7)
        ]
    else:
        start_date_value = selected_date
        end_date_value = selected_date
        dates = [selected_date]

    reservations = get_reservations_for_range(
        instrument_id,
        start_date_value,
        end_date_value,
    )

    blocked_periods = get_blocked_periods_for_range(
        instrument_id,
        start_date_value,
        end_date_value,
    )

    st.caption(
        "空いている時刻をクリックすると、その時刻から新規予約できます。"
    )

    render_calendar(
        dates,
        reservations,
        blocked_periods,
        instrument_id,
        compact_mode=mobile and view_mode == "週間",
    )

    st.divider()

    if st.button(
        "保存された利用者情報をクリア",
        use_container_width=mobile,
    ):
        queue_browser_profile_clear()
        st.rerun()


# ============================================================
# Manager login
# ============================================================

def render_login() -> bool:
    if st.session_state["logged_in"]:
        st.success(f"ログイン中：{st.session_state['display_name']}")

        if st.button("ログアウト"):
            logout()

        return True

    st.header("管理者ログイン")

    with st.form("login_form"):
        username = st.text_input("ユーザー名")
        password = st.text_input("パスワード", type="password")
        submitted = st.form_submit_button(
            "ログイン",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if authenticate(username, password):
            st.rerun()
        else:
            st.error("ユーザー名またはパスワードが正しくありません。")

    return False


# ============================================================
# System administrator
# ============================================================

def admin_instrument_management() -> None:
    st.subheader("機器管理")

    with st.form("add_instrument"):
        name = st.text_input("機器名 *")
        description = st.text_area("説明")
        notice = st.text_area("利用者への注意事項")
        submitted = st.form_submit_button("機器を追加")

    if submitted:
        if not name.strip():
            st.error("機器名を入力してください。")
        else:
            try:
                with get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO instruments (
                            name,
                            description,
                            notice
                        )
                        VALUES (?, ?, ?)
                        """,
                        (
                            name.strip(),
                            description.strip(),
                            notice.strip(),
                        ),
                    )
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("同名の機器が既に登録されています。")

    for instrument in get_instruments(active_only=False):
        with st.expander(instrument["name"]):
            new_name = st.text_input(
                "機器名",
                value=instrument["name"],
                key=f"admin_instrument_name_{instrument['id']}",
            )

            description = st.text_area(
                "説明",
                value=instrument["description"],
                key=f"admin_description_{instrument['id']}",
            )

            notice = st.text_area(
                "利用者への注意事項",
                value=instrument["notice"],
                key=f"admin_notice_{instrument['id']}",
            )

            active = st.checkbox(
                "予約可能",
                value=bool(instrument["active"]),
                key=f"admin_active_{instrument['id']}",
            )

            if st.button(
                "保存",
                key=f"admin_save_instrument_{instrument['id']}",
            ):
                try:
                    with get_connection() as conn:
                        conn.execute(
                            """
                            UPDATE instruments
                            SET
                                name = ?,
                                description = ?,
                                notice = ?,
                                active = ?
                            WHERE id = ?
                            """,
                            (
                                new_name.strip(),
                                description.strip(),
                                notice.strip(),
                                int(active),
                                instrument["id"],
                            ),
                        )
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("同名の機器が既に登録されています。")

            st.markdown("#### 機器削除")
            st.warning(
                "機器を削除すると、予約情報、入力項目、使用停止期間、"
                "管理者割り当ても削除されます。"
            )

            confirm = st.checkbox(
                "予約情報を含め、この機器を削除する",
                key=f"admin_confirm_delete_{instrument['id']}",
            )

            if st.button(
                "機器を削除",
                disabled=not confirm,
                key=f"admin_delete_instrument_{instrument['id']}",
            ):
                delete_instrument(instrument["id"])
                st.rerun()


def admin_manager_management() -> None:
    st.subheader("機器管理者")

    with st.form("add_manager"):
        username = st.text_input("ユーザー名 *")
        display_name = st.text_input("表示名 *")
        password = st.text_input("初期パスワード *", type="password")
        submitted = st.form_submit_button("機器管理者を追加")

    if submitted:
        if not username.strip() or not display_name.strip() or not password:
            st.error("すべての必須項目を入力してください。")
        else:
            salt, password_hash = make_hash(password)

            try:
                with get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO manager_accounts (
                            username,
                            display_name,
                            password_salt,
                            password_hash
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            username.strip(),
                            display_name.strip(),
                            salt,
                            password_hash,
                        ),
                    )
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("同じユーザー名が既に存在します。")

    with get_connection() as conn:
        managers = conn.execute(
            """
            SELECT *
            FROM manager_accounts
            ORDER BY display_name
            """
        ).fetchall()

    instruments = get_instruments(active_only=False)

    if managers and instruments:
        st.markdown("### 担当機器の割り当て")

        manager_map = {
            f"{row['display_name']}（{row['username']}）": row["id"]
            for row in managers
            if row["active"]
        }

        instrument_map = {
            row["name"]: row["id"]
            for row in instruments
        }

        if manager_map:
            with st.form("assign_instrument"):
                selected_manager = st.selectbox(
                    "機器管理者",
                    list(manager_map.keys()),
                )

                selected_instrument = st.selectbox(
                    "機器",
                    list(instrument_map.keys()),
                )

                submitted = st.form_submit_button("担当機器を割り当て")

            if submitted:
                try:
                    with get_connection() as conn:
                        conn.execute(
                            """
                            INSERT INTO instrument_managers (
                                instrument_id,
                                manager_id
                            )
                            VALUES (?, ?)
                            """,
                            (
                                instrument_map[selected_instrument],
                                manager_map[selected_manager],
                            ),
                        )
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("既に割り当て済みです。")

    st.markdown("### 登録済み機器管理者")

    if not managers:
        st.info("機器管理者は登録されていません。")
        return

    for manager in managers:
        with st.expander(
            f"{manager['display_name']}（{manager['username']}）"
        ):
            with get_connection() as conn:
                assigned = conn.execute(
                    """
                    SELECT
                        im.instrument_id,
                        im.manager_id,
                        i.name AS instrument_name
                    FROM instrument_managers im
                    JOIN instruments i
                        ON i.id = im.instrument_id
                    WHERE im.manager_id = ?
                    ORDER BY i.name
                    """,
                    (manager["id"],),
                ).fetchall()

            st.markdown("**担当機器**")

            if not assigned:
                st.caption("担当機器はありません。")

            for row in assigned:
                col1, col2 = st.columns([5, 1])

                with col1:
                    st.write(row["instrument_name"])

                with col2:
                    if st.button(
                        "解除",
                        key=(
                            "remove_assignment_"
                            f"{row['manager_id']}_"
                            f"{row['instrument_id']}"
                        ),
                    ):
                        with get_connection() as conn:
                            conn.execute(
                                """
                                DELETE FROM instrument_managers
                                WHERE
                                    manager_id = ?
                                    AND instrument_id = ?
                                """,
                                (
                                    row["manager_id"],
                                    row["instrument_id"],
                                ),
                            )
                        st.rerun()

            active = st.checkbox(
                "アカウントを有効にする",
                value=bool(manager["active"]),
                key=f"manager_active_{manager['id']}",
            )

            new_password = st.text_input(
                "新しいパスワード",
                type="password",
                key=f"manager_password_{manager['id']}",
                help="変更しない場合は空欄のままにしてください。",
            )

            if st.button(
                "アカウント設定を保存",
                key=f"save_manager_{manager['id']}",
            ):
                with get_connection() as conn:
                    conn.execute(
                        """
                        UPDATE manager_accounts
                        SET active = ?
                        WHERE id = ?
                        """,
                        (
                            int(active),
                            manager["id"],
                        ),
                    )

                    if new_password:
                        salt, password_hash = make_hash(new_password)

                        conn.execute(
                            """
                            UPDATE manager_accounts
                            SET
                                password_salt = ?,
                                password_hash = ?
                            WHERE id = ?
                            """,
                            (
                                salt,
                                password_hash,
                                manager["id"],
                            ),
                        )
                st.rerun()

            st.divider()
            st.markdown("**機器管理者の削除**")
            st.warning(
                "この機器管理者アカウントを削除すると、"
                "担当機器の割り当ても解除されます。"
            )

            confirm_delete = st.checkbox(
                f"「{manager['display_name']}」を削除する",
                key=f"confirm_delete_manager_{manager['id']}",
            )

            if st.button(
                "機器管理者を削除",
                disabled=not confirm_delete,
                key=f"delete_manager_{manager['id']}",
            ):
                with get_connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM manager_accounts
                        WHERE id = ?
                        """,
                        (manager["id"],),
                    )
                st.rerun()


# ============================================================
# Instrument manager
# ============================================================

def custom_field_management(instrument_id: int) -> None:
    st.markdown("### 予約入力項目")

    type_labels = {
        "一行テキスト": "text",
        "複数行テキスト": "textarea",
        "単一選択": "select",
        "複数選択": "multiselect",
        "数値": "number",
        "チェックボックス": "checkbox",
    }

    with st.form(f"custom_field_{instrument_id}"):
        field_name = st.text_input("項目名 *")
        type_label = st.selectbox("入力形式", list(type_labels.keys()))
        required = st.checkbox("必須項目にする")
        options_text = st.text_area(
            "選択肢",
            help="選択形式の場合、1行に1項目入力してください。",
        )
        submitted = st.form_submit_button("入力項目を追加")

    if submitted:
        field_type = type_labels[type_label]
        options = [
            line.strip()
            for line in options_text.splitlines()
            if line.strip()
        ]

        if not field_name.strip():
            st.error("項目名を入力してください。")
        elif field_type in {"select", "multiselect"} and not options:
            st.error("選択肢を入力してください。")
        else:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(MAX(display_order), 0) AS max_order
                    FROM custom_fields
                    WHERE instrument_id = ?
                    """,
                    (instrument_id,),
                ).fetchone()

                conn.execute(
                    """
                    INSERT INTO custom_fields (
                        instrument_id,
                        field_name,
                        field_type,
                        required,
                        options_json,
                        display_order
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        instrument_id,
                        field_name.strip(),
                        field_type,
                        int(required),
                        json.dumps(options, ensure_ascii=False),
                        row["max_order"] + 1,
                    ),
                )
            st.rerun()

    fields = get_custom_fields(instrument_id)

    if not fields:
        st.caption("追加の予約入力項目はありません。")

    for field in fields:
        col1, col2 = st.columns([6, 1])

        with col1:
            st.write(
                f"**{field['field_name']}** ｜ "
                f"{'必須' if field['required'] else '任意'}"
            )

        with col2:
            if st.button(
                "削除",
                key=f"delete_custom_field_{field['id']}",
            ):
                with get_connection() as conn:
                    conn.execute(
                        """
                        UPDATE custom_fields
                        SET active = 0
                        WHERE id = ?
                        """,
                        (field["id"],),
                    )
                st.rerun()


def blocked_period_management(instrument_id: int) -> None:
    st.markdown("### 使用停止期間")

    with st.form(f"blocked_{instrument_id}"):
        blocked_date = st.date_input(
            "日付",
            value=get_jst_now().date(),
            format="YYYY/MM/DD",
        )

        col1, col2 = st.columns(2)

        with col1:
            start_time_value = st.selectbox(
                "開始時刻",
                TIME_OPTIONS,
                index=36,
            )

        with col2:
            end_time_value = st.selectbox(
                "終了時刻",
                TIME_OPTIONS,
                index=68,
            )

        reason = st.text_input("理由")
        submitted = st.form_submit_button("使用停止期間を追加")

    if submitted:
        start_dt = combine_datetime(blocked_date, start_time_value)
        end_dt = combine_datetime(blocked_date, end_time_value)

        if end_dt <= start_dt:
            st.error("終了時刻は開始時刻より後にしてください。")
        else:
            conflict, message = reservation_has_conflict(
                instrument_id,
                start_dt,
                end_dt,
            )

            if conflict:
                st.error(message)
            else:
                with get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO blocked_periods (
                            instrument_id,
                            reservation_date,
                            start_time,
                            end_time,
                            reason
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            instrument_id,
                            blocked_date.isoformat(),
                            start_time_value,
                            end_time_value,
                            reason.strip(),
                        ),
                    )
                st.rerun()

    blocked_periods = get_blocked_periods(instrument_id)

    if blocked_periods:
        st.markdown("#### 登録済み使用停止期間")

    for blocked in blocked_periods:
        col1, col2 = st.columns([6, 1])

        with col1:
            label = (
                f"{blocked['reservation_date']} "
                f"{blocked['start_time']}～{blocked['end_time']}"
            )

            if blocked["reason"]:
                label += f" ｜ {blocked['reason']}"

            st.write(label)

        with col2:
            if st.button(
                "削除",
                key=f"delete_blocked_{blocked['id']}",
            ):
                with get_connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM blocked_periods
                        WHERE id = ?
                        """,
                        (blocked["id"],),
                    )
                st.rerun()


def manager_instrument_settings() -> None:
    instrument_ids = manageable_instrument_ids()

    instruments = [
        instrument
        for instrument in (
            get_instrument(instrument_id)
            for instrument_id in instrument_ids
        )
        if instrument is not None
    ]

    if not instruments:
        st.info("担当機器はありません。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    selected_name = st.selectbox(
        "管理する機器",
        list(instrument_map.keys()),
        key="managed_instrument_selector",
    )

    instrument_id = instrument_map[selected_name]
    instrument = get_instrument(instrument_id)

    if instrument is None:
        return

    st.markdown("### 基本設定")

    description = st.text_area(
        "機器の説明",
        value=instrument["description"],
        key=f"manager_description_{instrument_id}",
    )

    notice = st.text_area(
        "利用者への注意事項",
        value=instrument["notice"],
        key=f"manager_notice_{instrument_id}",
    )

    if st.button(
        "基本設定を保存",
        key=f"save_manager_instrument_{instrument_id}",
    ):
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE instruments
                SET
                    description = ?,
                    notice = ?
                WHERE id = ?
                """,
                (
                    description.strip(),
                    notice.strip(),
                    instrument_id,
                ),
            )
        st.rerun()

    st.divider()
    custom_field_management(instrument_id)
    st.divider()
    blocked_period_management(instrument_id)

    # 機器削除はadmin限定。


def manager_reservation_management() -> None:
    instrument_ids = manageable_instrument_ids()

    instruments = [
        instrument
        for instrument in (
            get_instrument(instrument_id)
            for instrument_id in instrument_ids
        )
        if instrument is not None
    ]

    if not instruments:
        st.info("管理できる機器はありません。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    selected_name = st.selectbox(
        "機器",
        list(instrument_map.keys()),
        key="manager_reservation_instrument",
    )

    reservations = get_reservations(instrument_map[selected_name])

    if not reservations:
        st.info("予約はありません。")
        return

    for reservation in reservations:
        with st.expander(
            f"{format_reservation_period(reservation)} ｜ "
            f"{reservation['user_name']}"
        ):
            st.write(f"**所属講座：** {reservation['affiliation']}")
            st.write(f"**使用目的：** {purpose_label(reservation)}")

            if reservation["remarks"]:
                st.write(f"**備考：** {reservation['remarks']}")

            for field_value in get_reservation_field_values(reservation["id"]):
                value = json.loads(field_value["value_json"])
                st.write(
                    f"**{field_value['field_name_snapshot']}：** "
                    f"{display_custom_value(value)}"
                )

            if st.button(
                "管理者権限で予約を削除",
                key=f"manager_delete_reservation_{reservation['id']}",
            ):
                delete_reservation(reservation["id"])
                st.rerun()


# ============================================================
# Management page
# ============================================================

def page_management() -> None:
    if not render_login():
        return

    render_own_password_change()

    if st.session_state["role"] == "system_admin":
        tabs = st.tabs(
            [
                "機器管理",
                "機器管理者",
                "担当機器設定",
                "予約管理",
            ]
        )

        with tabs[0]:
            admin_instrument_management()
        with tabs[1]:
            admin_manager_management()
        with tabs[2]:
            manager_instrument_settings()
        with tabs[3]:
            manager_reservation_management()
    else:
        tabs = st.tabs(
            [
                "担当機器設定",
                "予約管理",
            ]
        )

        with tabs[0]:
            manager_instrument_settings()
        with tabs[1]:
            manager_reservation_management()


# ============================================================
# Instrument selection
# ============================================================

def resolve_instrument_id(instruments: list[sqlite3.Row]) -> int:
    instrument_ids = [
        instrument["id"]
        for instrument in instruments
    ]

    requested_value = st.query_params.get("instrument_id")

    if requested_value:
        try:
            requested_id = int(requested_value)
            if requested_id in instrument_ids:
                return requested_id
        except ValueError:
            pass

    saved_instrument_id = st.session_state["saved_instrument_id"]

    if saved_instrument_id in instrument_ids:
        return saved_instrument_id

    return instrument_ids[0]


def change_instrument(instrument_id: int) -> None:
    request_scroll_top()
    st.query_params.clear()
    st.query_params["instrument_id"] = str(instrument_id)
    st.rerun()


def render_mobile_instrument_selector(
    instruments: list[sqlite3.Row],
    current_instrument_id: int,
) -> None:
    if not is_mobile_device():
        return

    st.markdown("### 機器")

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    instrument_names = list(instrument_map.keys())

    current_name = next(
        instrument["name"]
        for instrument in instruments
        if instrument["id"] == current_instrument_id
    )

    selected_name = st.radio(
        "機器",
        instrument_names,
        index=instrument_names.index(current_name),
        key=f"mobile_instrument_selector_{current_instrument_id}",
        label_visibility="collapsed",
    )

    selected_id = instrument_map[selected_name]

    if selected_id != current_instrument_id:
        change_instrument(selected_id)

    st.divider()


def render_sidebar_instrument_order_settings(
    instruments: list[sqlite3.Row],
) -> None:
    with st.sidebar.expander(
        "機器表示順"
    ):
        st.caption(
            "このブラウザでの機器表示順を変更できます。"
        )

        default_order = [
            instrument["id"]
            for instrument in instruments
        ]

        normalized_saved_order = (
            normalize_instrument_order(
                instruments
            )
        )

        draft_key = "instrument_order_draft"

        draft_order = st.session_state.get(
            draft_key
        )

        if (
            not isinstance(
                draft_order,
                list,
            )
            or set(draft_order)
            != set(default_order)
            or len(draft_order)
            != len(default_order)
        ):
            st.session_state[
                draft_key
            ] = normalized_saved_order.copy()

        instrument_by_id = {
            instrument["id"]: instrument
            for instrument in instruments
        }

        current_order = st.session_state[
            draft_key
        ].copy()

        for index, instrument_id in enumerate(
            current_order
        ):
            instrument = instrument_by_id[
                instrument_id
            ]

            col_name, col_up, col_down = st.columns(
                [5, 1, 1]
            )

            with col_name:
                st.write(
                    instrument["name"]
                )

            with col_up:
                if st.button(
                    "↑",
                    disabled=index == 0,
                    key=(
                        "sidebar_instrument_order_up_"
                        f"{instrument_id}"
                    ),
                    help="上へ移動",
                    use_container_width=True,
                ):
                    current_order[
                        index - 1
                    ], current_order[
                        index
                    ] = (
                        current_order[index],
                        current_order[index - 1],
                    )

                    st.session_state[
                        draft_key
                    ] = current_order

                    st.rerun()

            with col_down:
                if st.button(
                    "↓",
                    disabled=(
                        index
                        == len(
                            current_order
                        ) - 1
                    ),
                    key=(
                        "sidebar_instrument_order_down_"
                        f"{instrument_id}"
                    ),
                    help="下へ移動",
                    use_container_width=True,
                ):
                    current_order[
                        index + 1
                    ], current_order[
                        index
                    ] = (
                        current_order[index],
                        current_order[index + 1],
                    )

                    st.session_state[
                        draft_key
                    ] = current_order

                    st.rerun()

        if st.button(
            "表示順を保存",
            type="primary",
            use_container_width=True,
            key="sidebar_save_instrument_order",
        ):
            queue_instrument_order_save(
                st.session_state[
                    draft_key
                ]
            )

            st.rerun()

        if st.button(
            "初期順に戻す",
            use_container_width=True,
            key="sidebar_reset_instrument_order",
        ):
            st.session_state[
                draft_key
            ] = default_order.copy()

            queue_instrument_order_reset()

            st.rerun()


# ============================================================
# Main
# ============================================================

def main() -> None:
    init_db()
    init_session()

    if not load_browser_profile():
        st.info("ブラウザ情報を読み込んでいます...")
        st.stop()

    if not flush_pending_browser_action():
        st.info("利用者情報を保存しています...")
        st.stop()

    flush_scroll_top()

    if not render_access_gate():
        return

    st.title(APP_TITLE)

    page = st.sidebar.radio(
        "メニュー",
        [
            "予約・予約状況",
            "管理者",
        ],
    )

    with st.sidebar.expander("利用端末の認証"):
        st.caption(
            "この端末に保存された利用認証を解除します。"
        )

        if st.button(
            "利用認証を解除",
            use_container_width=True,
            key="clear_access_authorization",
        ):
            queue_access_authorization_clear()
            st.rerun()

    if page == "管理者":
        page_management()
        return

    base_instruments = get_instruments(
        active_only=True
    )

    if not base_instruments:
        st.info("現在、予約可能な機器は登録されていません。")
        return

    instruments = sort_instruments_for_browser(
        base_instruments
    )

    current_instrument_id = resolve_instrument_id(
        instruments
    )

    # スマホでは上部の機器選択のみを使用する。
    # サイドバー側の機器選択を同時に描画すると、
    # 2つのradioが独立したSession Stateを持ち、
    # query_paramsによる画面遷移を打ち消すことがある。
    if not is_mobile_device():
        st.sidebar.divider()
        st.sidebar.subheader("機器選択")

        instrument_map = {
            instrument["name"]: instrument["id"]
            for instrument in instruments
        }

        instrument_names = list(instrument_map.keys())

        current_name = next(
            instrument["name"]
            for instrument in instruments
            if instrument["id"] == current_instrument_id
        )

        selected_name = st.sidebar.radio(
            "予約する機器",
            instrument_names,
            index=instrument_names.index(current_name),
            key="sidebar_instrument_selector",
            label_visibility="collapsed",
        )

        selected_instrument_id = instrument_map[selected_name]

        if selected_instrument_id != current_instrument_id:
            change_instrument(selected_instrument_id)

    st.sidebar.divider()

    render_sidebar_instrument_order_settings(
        base_instruments
    )

    render_mobile_instrument_selector(
        instruments,
        current_instrument_id,
    )

    view = st.query_params.get("view", "")
    reservation_id_text = st.query_params.get("reservation_id", "")

    if view == "new":
        render_new_reservation_page(current_instrument_id)
        return

    if (
        view in {"reservation", "edit"}
        and reservation_id_text
    ):
        try:
            reservation_id = int(reservation_id_text)
        except ValueError:
            st.error("予約IDが正しくありません。")
            return

        if view == "reservation":
            render_reservation_detail_page(
                current_instrument_id,
                reservation_id,
            )
        else:
            render_edit_reservation_page(
                current_instrument_id,
                reservation_id,
            )

        return

    render_booking_page(
        current_instrument_id
    )


if __name__ == "__main__":
    main()
