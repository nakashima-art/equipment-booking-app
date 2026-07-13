import hashlib
import hmac
import html
import json
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

import streamlit as st
from streamlit_js_eval import streamlit_js_eval


# ============================================================
# Basic settings
# ============================================================

APP_TITLE = "共通機器予約システム（愛知学院大学薬学部）"
DB_PATH = "equipment_booking.db"

SYSTEM_ADMIN_USERNAME = "admin"
SYSTEM_ADMIN_PASSWORD = "1234"

SLOT_MINUTES = 15
SLOT_HEIGHT = 24

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧪",
    layout="wide",
)


# ============================================================
# Database
# ============================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {
        row["name"]
        for row in rows
    }


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

            CREATE TABLE IF NOT EXISTS instrument_managers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                manager_id INTEGER NOT NULL,
                UNIQUE(instrument_id, manager_id),
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (manager_id)
                    REFERENCES manager_accounts(id)
                    ON DELETE CASCADE
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
                    REFERENCES instruments(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                affiliation TEXT NOT NULL,
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
                    REFERENCES instruments(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reservation_field_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER NOT NULL,
                custom_field_id INTEGER,
                field_name_snapshot TEXT NOT NULL,
                field_type_snapshot TEXT NOT NULL,
                value_json TEXT NOT NULL,
                FOREIGN KEY (reservation_id)
                    REFERENCES reservations(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (custom_field_id)
                    REFERENCES custom_fields(id)
                    ON DELETE SET NULL
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
                    REFERENCES instruments(id)
                    ON DELETE CASCADE
            );
            """
        )

        # ----------------------------------------------------
        # Migration from the earlier reservation schema
        # ----------------------------------------------------

        columns = table_columns(
            conn,
            "reservations",
        )

        if "start_date" not in columns:
            conn.execute(
                """
                ALTER TABLE reservations
                ADD COLUMN start_date TEXT
                """
            )

        if "end_date" not in columns:
            conn.execute(
                """
                ALTER TABLE reservations
                ADD COLUMN end_date TEXT
                """
            )

        if "remarks" not in columns:
            conn.execute(
                """
                ALTER TABLE reservations
                ADD COLUMN remarks TEXT
                NOT NULL DEFAULT ''
                """
            )

        columns = table_columns(
            conn,
            "reservations",
        )

        if "reservation_date" in columns:
            conn.execute(
                """
                UPDATE reservations
                SET start_date = reservation_date
                WHERE
                    start_date IS NULL
                    OR start_date = ''
                """
            )

            conn.execute(
                """
                UPDATE reservations
                SET end_date = reservation_date
                WHERE
                    end_date IS NULL
                    OR end_date = ''
                """
            )


# ============================================================
# Security helpers
# ============================================================

def make_hash(
    value: str,
    salt_hex: str | None = None,
) -> tuple[str, str]:

    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()

    else:
        salt = bytes.fromhex(
            salt_hex
        )

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        200_000,
    ).hex()

    return salt_hex, digest


def verify_hash(
    value: str,
    salt_hex: str,
    stored_hash: str,
) -> bool:

    _, calculated = make_hash(
        value,
        salt_hex,
    )

    return hmac.compare_digest(
        calculated,
        stored_hash,
    )


# ============================================================
# Date and time helpers
# ============================================================

def generate_time_options() -> list[str]:
    result = []

    current = datetime.combine(
        date.today(),
        time(0, 0),
    )

    for _ in range(96):
        result.append(
            current.strftime("%H:%M")
        )

        current += timedelta(
            minutes=15
        )

    return result


TIME_OPTIONS = generate_time_options()


def to_minutes(
    value: str,
) -> int:

    hour, minute = map(
        int,
        value.split(":"),
    )

    return (
        hour * 60
        + minute
    )


def combine_datetime(
    target_date: date,
    target_time: str,
) -> datetime:

    hour, minute = map(
        int,
        target_time.split(":"),
    )

    return datetime.combine(
        target_date,
        time(
            hour=hour,
            minute=minute,
        ),
    )


def reservation_start_datetime(
    reservation: sqlite3.Row,
) -> datetime:

    return combine_datetime(
        date.fromisoformat(
            reservation["start_date"]
        ),
        reservation["start_time"],
    )


def reservation_end_datetime(
    reservation: sqlite3.Row,
) -> datetime:

    return combine_datetime(
        date.fromisoformat(
            reservation["end_date"]
        ),
        reservation["end_time"],
    )


def get_week_start(
    target_date: date,
) -> date:

    return (
        target_date
        - timedelta(
            days=target_date.weekday()
        )
    )


def format_japanese_date(
    target_date: date,
) -> str:

    weekdays = [
        "月",
        "火",
        "水",
        "木",
        "金",
        "土",
        "日",
    ]

    return (
        f"{target_date.month}/"
        f"{target_date.day}"
        f"（{weekdays[target_date.weekday()]}）"
    )


def format_reservation_period(
    reservation: sqlite3.Row,
) -> str:

    start_date = date.fromisoformat(
        reservation["start_date"]
    )

    end_date = date.fromisoformat(
        reservation["end_date"]
    )

    return (
        f"{start_date.strftime('%Y/%m/%d')} "
        f"{reservation['start_time']}"
        " ～ "
        f"{end_date.strftime('%Y/%m/%d')} "
        f"{reservation['end_time']}"
    )


def intervals_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:

    return (
        start_a < end_b
        and
        end_a > start_b
    )


# ============================================================
# Browser storage
# ============================================================

def read_local_storage(
    key: str,
) -> str:

    value = streamlit_js_eval(
        js_expressions=(
            f"localStorage.getItem("
            f"{json.dumps(key)}"
            f")"
        ),
        key=f"read_{key}",
    )

    if value is None:
        return ""

    return str(value)


def write_local_storage(
    key: str,
    value: str,
) -> None:

    streamlit_js_eval(
        js_expressions=(
            f"localStorage.setItem("
            f"{json.dumps(key)}, "
            f"{json.dumps(value)}"
            f")"
        ),
        key=(
            f"write_{key}_"
            f"{hash(value)}"
        ),
    )


def clear_local_storage() -> None:

    streamlit_js_eval(
        js_expressions="""
        localStorage.removeItem(
            "equipment_booking_name"
        );
        localStorage.removeItem(
            "equipment_booking_affiliation"
        );
        localStorage.removeItem(
            "equipment_booking_last_instrument"
        );
        true;
        """,
        key="clear_saved_user",
    )


# ============================================================
# Instrument data
# ============================================================

def get_instruments(
    active_only: bool = True,
) -> list[sqlite3.Row]:

    query = """
        SELECT *
        FROM instruments
    """

    if active_only:
        query += """
            WHERE active = 1
        """

    query += """
        ORDER BY name
    """

    with get_connection() as conn:
        return conn.execute(
            query
        ).fetchall()


def get_instrument(
    instrument_id: int,
) -> sqlite3.Row | None:

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM instruments
            WHERE id = ?
            """,
            (instrument_id,),
        ).fetchone()


def delete_instrument(
    instrument_id: int,
) -> None:

    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM instruments
            WHERE id = ?
            """,
            (instrument_id,),
        )


# ============================================================
# Custom fields
# ============================================================

def get_custom_fields(
    instrument_id: int,
) -> list[sqlite3.Row]:

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM custom_fields
            WHERE
                instrument_id = ?
                AND active = 1
            ORDER BY
                display_order,
                id
            """,
            (instrument_id,),
        ).fetchall()


def get_reservation_field_values(
    reservation_id: int,
) -> list[sqlite3.Row]:

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


def get_reservation_field_value_map(
    reservation_id: int,
) -> dict[int, Any]:

    rows = get_reservation_field_values(
        reservation_id
    )

    result = {}

    for row in rows:
        if row["custom_field_id"] is None:
            continue

        result[
            row["custom_field_id"]
        ] = json.loads(
            row["value_json"]
        )

    return result


# ============================================================
# Reservations
# ============================================================

def get_reservation(
    reservation_id: int,
) -> sqlite3.Row | None:

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                r.*,
                i.name AS instrument_name
            FROM reservations r
            JOIN instruments i
                ON i.id = r.instrument_id
            WHERE r.id = ?
            """,
            (reservation_id,),
        ).fetchone()


def get_reservations(
    instrument_id: int,
) -> list[sqlite3.Row]:

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                r.*,
                i.name AS instrument_name
            FROM reservations r
            JOIN instruments i
                ON i.id = r.instrument_id
            WHERE r.instrument_id = ?
            ORDER BY
                r.start_date,
                r.start_time
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
            SELECT
                r.*,
                i.name AS instrument_name
            FROM reservations r
            JOIN instruments i
                ON i.id = r.instrument_id
            WHERE
                r.instrument_id = ?
                AND r.start_date <= ?
                AND r.end_date >= ?
            ORDER BY
                r.start_date,
                r.start_time
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

    reservations = get_reservations(
        instrument_id
    )

    for reservation in reservations:

        if (
            exclude_reservation_id is not None
            and
            reservation["id"]
            == exclude_reservation_id
        ):
            continue

        existing_start = (
            reservation_start_datetime(
                reservation
            )
        )

        existing_end = (
            reservation_end_datetime(
                reservation
            )
        )

        if intervals_overlap(
            start_dt,
            end_dt,
            existing_start,
            existing_end,
        ):

            return (
                True,
                "指定した期間には既に予約があります。",
            )

    blocked_periods = get_blocked_periods(
        instrument_id
    )

    for blocked in blocked_periods:

        blocked_date = date.fromisoformat(
            blocked["reservation_date"]
        )

        blocked_start = combine_datetime(
            blocked_date,
            blocked["start_time"],
        )

        blocked_end = combine_datetime(
            blocked_date,
            blocked["end_time"],
        )

        if intervals_overlap(
            start_dt,
            end_dt,
            blocked_start,
            blocked_end,
        ):

            message = (
                "指定した期間には使用停止時間が"
                "含まれています。"
            )

            if blocked["reason"]:
                message += (
                    f" 理由：{blocked['reason']}"
                )

            return True, message

    return False, ""


def save_reservation_field_values(
    conn: sqlite3.Connection,
    reservation_id: int,
    instrument_id: int,
    custom_values: dict[int, Any],
) -> None:

    conn.execute(
        """
        DELETE FROM reservation_field_values
        WHERE reservation_id = ?
        """,
        (reservation_id,),
    )

    fields = {
        field["id"]: field
        for field in get_custom_fields(
            instrument_id
        )
    }

    for field_id, value in custom_values.items():

        field = fields.get(
            field_id
        )

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
                json.dumps(
                    value,
                    ensure_ascii=False,
                ),
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
) -> None:

    pin_salt, pin_hash = make_hash(
        pin
    )

    with get_connection() as conn:

        cursor = conn.execute(
            """
            INSERT INTO reservations (
                instrument_id,
                user_name,
                affiliation,
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
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                instrument_id,
                user_name.strip(),
                affiliation.strip(),
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

        reservation_id = cursor.lastrowid

        save_reservation_field_values(
            conn,
            reservation_id,
            instrument_id,
            custom_values,
        )


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

    reservation = get_reservation(
        reservation_id
    )

    if reservation is None:
        return

    instrument_id = reservation[
        "instrument_id"
    ]

    with get_connection() as conn:

        conn.execute(
            """
            UPDATE reservations
            SET
                user_name = ?,
                affiliation = ?,
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


def delete_reservation(
    reservation_id: int,
) -> None:

    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM reservations
            WHERE id = ?
            """,
            (reservation_id,),
        )


# ============================================================
# Blocked periods
# ============================================================

def get_blocked_periods(
    instrument_id: int,
) -> list[sqlite3.Row]:

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM blocked_periods
            WHERE instrument_id = ?
            ORDER BY
                reservation_date,
                start_time
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
            ORDER BY
                reservation_date,
                start_time
            """,
            (
                instrument_id,
                range_start.isoformat(),
                range_end.isoformat(),
            ),
        ).fetchall()


# ============================================================
# Login
# ============================================================

def init_session() -> None:

    defaults = {
        "logged_in": False,
        "role": None,
        "manager_id": None,
        "username": None,
        "display_name": None,
        "edit_reservation_id": None,
        "verified_reservation_id": None,
    }

    for key, value in defaults.items():
        st.session_state.setdefault(
            key,
            value,
        )


def logout() -> None:

    st.session_state[
        "logged_in"
    ] = False

    st.session_state[
        "role"
    ] = None

    st.session_state[
        "manager_id"
    ] = None

    st.session_state[
        "username"
    ] = None

    st.session_state[
        "display_name"
    ] = None

    st.rerun()


def authenticate(
    username: str,
    password: str,
) -> bool:

    username = username.strip()

    if (
        username
        == SYSTEM_ADMIN_USERNAME
        and
        password
        == SYSTEM_ADMIN_PASSWORD
    ):

        st.session_state[
            "logged_in"
        ] = True

        st.session_state[
            "role"
        ] = "system_admin"

        st.session_state[
            "display_name"
        ] = "システム管理者"

        st.session_state[
            "username"
        ] = username

        return True

    with get_connection() as conn:

        account = conn.execute(
            """
            SELECT *
            FROM manager_accounts
            WHERE
                username = ?
                AND active = 1
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

    st.session_state[
        "logged_in"
    ] = True

    st.session_state[
        "role"
    ] = "instrument_manager"

    st.session_state[
        "manager_id"
    ] = account["id"]

    st.session_state[
        "username"
    ] = account["username"]

    st.session_state[
        "display_name"
    ] = account["display_name"]

    return True


def get_managed_instrument_ids(
    manager_id: int,
) -> list[int]:

    with get_connection() as conn:

        rows = conn.execute(
            """
            SELECT instrument_id
            FROM instrument_managers
            WHERE manager_id = ?
            """,
            (manager_id,),
        ).fetchall()

    return [
        row["instrument_id"]
        for row in rows
    ]


def manageable_instrument_ids() -> list[int]:

    if (
        st.session_state["role"]
        == "system_admin"
    ):

        return [
            row["id"]
            for row in get_instruments(
                active_only=False
            )
        ]

    manager_id = st.session_state[
        "manager_id"
    ]

    if manager_id is None:
        return []

    return get_managed_instrument_ids(
        manager_id
    )


# ============================================================
# Display helpers
# ============================================================

def purpose_label(
    reservation: sqlite3.Row,
) -> str:

    if reservation["purpose"] == "その他":

        detail = reservation[
            "purpose_other"
        ].strip()

        if detail:
            return (
                f"その他（{detail}）"
            )

    return reservation["purpose"]


def field_value_is_empty(
    field: sqlite3.Row,
    value: Any,
) -> bool:

    if field["field_type"] == "checkbox":
        return value is False

    if field["field_type"] == "multiselect":
        return len(value) == 0

    if value is None:
        return True

    if isinstance(
        value,
        str,
    ):
        return (
            value.strip() == ""
        )

    return False


def render_custom_field(
    field: sqlite3.Row,
    key_prefix: str,
    default_value: Any = None,
) -> Any:

    label = field["field_name"]

    if field["required"]:
        label += " *"

    widget_key = (
        f"{key_prefix}_"
        f"{field['id']}"
    )

    field_type = field[
        "field_type"
    ]

    options = json.loads(
        field["options_json"]
        or "[]"
    )

    if field_type == "text":

        return st.text_input(
            label,
            value=(
                str(default_value)
                if default_value is not None
                else ""
            ),
            key=widget_key,
        )

    if field_type == "textarea":

        return st.text_area(
            label,
            value=(
                str(default_value)
                if default_value is not None
                else ""
            ),
            key=widget_key,
        )

    if field_type == "select":

        select_options = [
            "選択してください"
        ] + options

        index = 0

        if default_value in options:
            index = (
                options.index(
                    default_value
                )
                + 1
            )

        selected = st.selectbox(
            label,
            select_options,
            index=index,
            key=widget_key,
        )

        if (
            selected
            == "選択してください"
        ):
            return ""

        return selected

    if field_type == "multiselect":

        defaults = (
            default_value
            if isinstance(
                default_value,
                list,
            )
            else []
        )

        return st.multiselect(
            label,
            options,
            default=[
                value
                for value in defaults
                if value in options
            ],
            key=widget_key,
        )

    if field_type == "number":

        try:
            number_value = float(
                default_value
            )

        except (
            TypeError,
            ValueError,
        ):
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
            value=bool(
                default_value
            ),
            key=widget_key,
        )

    return ""


# ============================================================
# Calendar
# ============================================================

def calendar_times() -> list[str]:
    return TIME_OPTIONS.copy()


CALENDAR_TIMES = calendar_times()


def reservation_segment_for_date(
    reservation: sqlite3.Row,
    target_date: date,
) -> tuple[int, int] | None:

    reservation_start = (
        reservation_start_datetime(
            reservation
        )
    )

    reservation_end = (
        reservation_end_datetime(
            reservation
        )
    )

    day_start = datetime.combine(
        target_date,
        time(0, 0),
    )

    day_end = (
        day_start
        + timedelta(days=1)
    )

    segment_start = max(
        reservation_start,
        day_start,
    )

    segment_end = min(
        reservation_end,
        day_end,
    )

    if (
        segment_start
        >= segment_end
    ):
        return None

    start_minutes = int(
        (
            segment_start
            - day_start
        ).total_seconds()
        // 60
    )

    end_minutes = int(
        (
            segment_end
            - day_start
        ).total_seconds()
        // 60
    )

    return (
        start_minutes,
        end_minutes,
    )


def build_calendar_html(
    dates: list[date],
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
) -> str:

    column_count = len(dates)
    slot_count = 96

    minimum_width = (
        90
        + column_count * 165
    )

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
        border: 1px solid #d9d9d9;
        border-radius: 8px;
        background: white;
    }}

    .calendar {{
        min-width: {minimum_width}px;
        display: grid;
        grid-template-columns:
            80px repeat(
                {column_count},
                minmax(155px, 1fr)
            );
        grid-template-rows:
            48px repeat(
                {slot_count},
                {SLOT_HEIGHT}px
            );
        position: relative;
        font-family:
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            sans-serif;
        font-size: 13px;
    }}

    .header {{
        position: sticky;
        top: 0;
        z-index: 10;
        padding: 10px 4px;
        text-align: center;
        font-weight: 600;
        border-right: 1px solid #ddd;
        border-bottom: 1px solid #bbb;
        background: #f7f7f7;
        box-sizing: border-box;
    }}

    .time {{
        padding: 3px 6px;
        text-align: right;
        color: #666;
        font-size: 11px;
        border-right: 1px solid #ddd;
        border-bottom: 1px solid #eee;
        background: #fafafa;
        box-sizing: border-box;
    }}

    .cell {{
        border-right: 1px solid #e0e0e0;
        border-bottom: 1px solid #eeeeee;
        background: white;
        box-sizing: border-box;
    }}

    .hour {{
        border-top: 1px solid #bdbdbd;
    }}

    .reservation {{
        z-index: 3;
        margin: 2px;
        padding: 4px 6px;
        border-radius: 4px;
        background: #dbeafe;
        border-left: 4px solid #2563eb;
        color: #1f2937;
        box-sizing: border-box;
        overflow: hidden;
        line-height: 1.25;
    }}

    .blocked {{
        z-index: 3;
        margin: 2px;
        padding: 4px 6px;
        border-radius: 4px;
        background: #f3f4f6;
        border-left: 4px solid #6b7280;
        color: #374151;
        box-sizing: border-box;
        overflow: hidden;
        line-height: 1.25;
    }}

    .name {{
        font-weight: 600;
    }}

    .small {{
        font-size: 11px;
    }}
    </style>
    """

    content = [
        css,
        '<div class="calendar-scroll">',
        '<div class="calendar">',
    ]

    content.append(
        """
        <div
            class="header"
            style="
                grid-column: 1;
                grid-row: 1;
            "
        >
            時刻
        </div>
        """
    )

    for day_index, target_date in enumerate(
        dates
    ):

        grid_column = (
            day_index + 2
        )

        label = html.escape(
            format_japanese_date(
                target_date
            )
        )

        content.append(
            f"""
            <div
                class="header"
                style="
                    grid-column: {grid_column};
                    grid-row: 1;
                "
            >
                {label}
            </div>
            """
        )

    # Background grid
    for slot_index, slot_time in enumerate(
        CALENDAR_TIMES
    ):

        grid_row = (
            slot_index + 2
        )

        minute = int(
            slot_time.split(":")[1]
        )

        hour_class = (
            " hour"
            if minute == 0
            else ""
        )

        time_label = (
            slot_time
            if minute in {
                0,
                30,
            }
            else ""
        )

        content.append(
            f"""
            <div
                class="time{hour_class}"
                style="
                    grid-column: 1;
                    grid-row: {grid_row};
                "
            >
                {time_label}
            </div>
            """
        )

        for day_index in range(
            column_count
        ):

            grid_column = (
                day_index + 2
            )

            content.append(
                f"""
                <div
                    class="cell{hour_class}"
                    style="
                        grid-column: {grid_column};
                        grid-row: {grid_row};
                    "
                ></div>
                """
            )

    # Reservations
    for reservation in reservations:

        for day_index, target_date in enumerate(
            dates
        ):

            segment = (
                reservation_segment_for_date(
                    reservation,
                    target_date,
                )
            )

            if segment is None:
                continue

            start_minutes, end_minutes = (
                segment
            )

            start_slot = (
                start_minutes
                // SLOT_MINUTES
            )

            end_slot = (
                end_minutes
                // SLOT_MINUTES
            )

            span = max(
                1,
                end_slot - start_slot,
            )

            grid_column = (
                day_index + 2
            )

            grid_row = (
                start_slot + 2
            )

            name = html.escape(
                reservation["user_name"]
            )

            purpose = html.escape(
                purpose_label(
                    reservation
                )
            )

            time_text = html.escape(
                f"{reservation['start_time']}"
                " ～ "
                f"{reservation['end_time']}"
            )

            content.append(
                f"""
                <div
                    class="reservation"
                    style="
                        grid-column: {grid_column};
                        grid-row:
                            {grid_row}
                            / span {span};
                    "
                >
                    <div class="name">
                        {name}
                    </div>

                    <div class="small">
                        {purpose}
                    </div>

                    <div class="small">
                        {time_text}
                    </div>
                </div>
                """
            )

    # Blocked periods
    for blocked in blocked_periods:

        blocked_date = date.fromisoformat(
            blocked["reservation_date"]
        )

        if blocked_date not in dates:
            continue

        day_index = dates.index(
            blocked_date
        )

        start_slot = (
            to_minutes(
                blocked["start_time"]
            )
            // SLOT_MINUTES
        )

        end_slot = (
            to_minutes(
                blocked["end_time"]
            )
            // SLOT_MINUTES
        )

        span = max(
            1,
            end_slot - start_slot,
        )

        grid_column = (
            day_index + 2
        )

        grid_row = (
            start_slot + 2
        )

        reason = html.escape(
            blocked["reason"]
            or "使用停止"
        )

        content.append(
            f"""
            <div
                class="blocked"
                style="
                    grid-column: {grid_column};
                    grid-row:
                        {grid_row}
                        / span {span};
                "
            >
                <div class="name">
                    使用停止
                </div>

                <div class="small">
                    {reason}
                </div>
            </div>
            """
        )

    content.append(
        "</div>"
    )

    content.append(
        "</div>"
    )

    return "".join(
        content
    )


def render_calendar(
    dates: list[date],
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
) -> None:

    calendar_html = (
        build_calendar_html(
            dates,
            reservations,
            blocked_periods,
        )
    )

    height = (
        96 * SLOT_HEIGHT
        + 65
    )

    st.components.v1.html(
        calendar_html,
        height=height,
        scrolling=False,
    )


# ============================================================
# New reservation
# ============================================================

def sync_new_end_date() -> None:

    st.session_state[
        "new_end_date"
    ] = st.session_state[
        "new_start_date"
    ]


def render_new_reservation_form(
    instrument_id: int,
) -> None:

    instrument = get_instrument(
        instrument_id
    )

    if instrument is None:
        return

    st.markdown(
        "### 新規予約"
    )

    if instrument["description"]:
        st.caption(
            instrument["description"]
        )

    if instrument["notice"]:
        st.info(
            instrument["notice"]
        )

    saved_name = read_local_storage(
        "equipment_booking_name"
    )

    saved_affiliation = (
        read_local_storage(
            "equipment_booking_affiliation"
        )
    )

    st.session_state.setdefault(
        "new_name",
        saved_name,
    )

    st.session_state.setdefault(
        "new_affiliation",
        saved_affiliation,
    )

    st.session_state.setdefault(
        "new_start_date",
        date.today(),
    )

    st.session_state.setdefault(
        "new_end_date",
        st.session_state[
            "new_start_date"
        ],
    )

    user_name = st.text_input(
        "氏名 *",
        key="new_name",
    )

    affiliation = st.text_input(
        "所属講座 *",
        key="new_affiliation",
    )

    col1, col2 = st.columns(2)

    with col1:

        start_date = st.date_input(
            "開始日 *",
            min_value=date.today(),
            key="new_start_date",
            format="YYYY/MM/DD",
            on_change=sync_new_end_date,
        )

    with col2:

        end_date = st.date_input(
            "終了日 *",
            min_value=date.today(),
            key="new_end_date",
            format="YYYY/MM/DD",
        )

    col1, col2 = st.columns(2)

    with col1:

        start_time = st.selectbox(
            "開始時刻 *",
            TIME_OPTIONS,
            index=36,
            key="new_start_time",
        )

    with col2:

        end_time = st.selectbox(
            "終了時刻 *",
            TIME_OPTIONS,
            index=40,
            key="new_end_time",
        )

    purpose = st.selectbox(
        "使用目的 *",
        [
            "測定",
            "解析のみ",
            "その他",
        ],
        key="new_purpose",
    )

    purpose_other = ""

    if purpose == "その他":

        purpose_other = st.text_input(
            "「その他」の内容 *",
            key="new_purpose_other",
        )

    remarks = st.text_area(
        "備考",
        key="new_remarks",
    )

    fields = get_custom_fields(
        instrument_id
    )

    custom_values = {}

    if fields:

        st.markdown(
            "#### 機器固有の入力項目"
        )

        for field in fields:

            custom_values[
                field["id"]
            ] = render_custom_field(
                field,
                "new_field",
            )

    pin = st.text_input(
        "4桁の暗証番号 *",
        type="password",
        max_chars=4,
        key="new_pin",
        help=(
            "予約の編集・取消時に必要です。"
        ),
    )

    if st.button(
        "予約する",
        type="primary",
        use_container_width=True,
        key="submit_new_reservation",
    ):

        errors = []

        start_dt = combine_datetime(
            start_date,
            start_time,
        )

        end_dt = combine_datetime(
            end_date,
            end_time,
        )

        now = datetime.now()

        if not user_name.strip():

            errors.append(
                "氏名を入力してください。"
            )

        if not affiliation.strip():

            errors.append(
                "所属講座を入力してください。"
            )

        if start_dt < now:

            errors.append(
                "過去の日時から予約を"
                "開始することはできません。"
            )

        if end_dt <= start_dt:

            errors.append(
                "終了日時は開始日時より"
                "後に設定してください。"
            )

        if (
            purpose == "その他"
            and
            not purpose_other.strip()
        ):

            errors.append(
                "「その他」の内容を"
                "入力してください。"
            )

        if not (
            pin.isdigit()
            and
            len(pin) == 4
        ):

            errors.append(
                "暗証番号は4桁の数字で"
                "入力してください。"
            )

        for field in fields:

            if (
                field["required"]
                and
                field_value_is_empty(
                    field,
                    custom_values[
                        field["id"]
                    ],
                )
            ):

                errors.append(
                    f"「{field['field_name']}」"
                    "を入力してください。"
                )

        if errors:

            for error in errors:
                st.error(error)

            return

        conflict, message = (
            reservation_has_conflict(
                instrument_id,
                start_dt,
                end_dt,
            )
        )

        if conflict:

            st.error(message)

            return

        add_reservation(
            instrument_id=instrument_id,
            user_name=user_name,
            affiliation=affiliation,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            purpose=purpose,
            purpose_other=purpose_other,
            remarks=remarks,
            pin=pin,
            custom_values=custom_values,
        )

        write_local_storage(
            "equipment_booking_name",
            user_name.strip(),
        )

        write_local_storage(
            "equipment_booking_affiliation",
            affiliation.strip(),
        )

        write_local_storage(
            "equipment_booking_last_instrument",
            str(instrument_id),
        )

        st.success(
            "予約しました。"
        )


# ============================================================
# Edit reservation
# ============================================================

def render_edit_reservation(
    reservation_id: int,
) -> None:

    reservation = get_reservation(
        reservation_id
    )

    if reservation is None:
        return

    if (
        reservation_end_datetime(
            reservation
        )
        <= datetime.now()
    ):

        st.error(
            "この予約は既に終了しているため、"
            "編集できません。"
        )

        st.session_state[
            "edit_reservation_id"
        ] = None

        return

    st.markdown(
        "### 予約編集"
    )

    field_values = (
        get_reservation_field_value_map(
            reservation_id
        )
    )

    fields = get_custom_fields(
        reservation["instrument_id"]
    )

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

    col1, col2 = st.columns(2)

    with col1:

        start_date = st.date_input(
            "開始日 *",
            value=date.fromisoformat(
                reservation["start_date"]
            ),
            key=f"edit_start_date_{reservation_id}",
            format="YYYY/MM/DD",
        )

    with col2:

        end_date = st.date_input(
            "終了日 *",
            value=date.fromisoformat(
                reservation["end_date"]
            ),
            key=f"edit_end_date_{reservation_id}",
            format="YYYY/MM/DD",
        )

    col1, col2 = st.columns(2)

    with col1:

        start_time = st.selectbox(
            "開始時刻 *",
            TIME_OPTIONS,
            index=TIME_OPTIONS.index(
                reservation["start_time"]
            ),
            key=f"edit_start_time_{reservation_id}",
        )

    with col2:

        end_time = st.selectbox(
            "終了時刻 *",
            TIME_OPTIONS,
            index=TIME_OPTIONS.index(
                reservation["end_time"]
            ),
            key=f"edit_end_time_{reservation_id}",
        )

    purpose_options = [
        "測定",
        "解析のみ",
        "その他",
    ]

    purpose = st.selectbox(
        "使用目的 *",
        purpose_options,
        index=purpose_options.index(
            reservation["purpose"]
        ),
        key=f"edit_purpose_{reservation_id}",
    )

    purpose_other = (
        reservation["purpose_other"]
    )

    if purpose == "その他":

        purpose_other = st.text_input(
            "「その他」の内容 *",
            value=reservation[
                "purpose_other"
            ],
            key=f"edit_other_{reservation_id}",
        )

    remarks = st.text_area(
        "備考",
        value=reservation["remarks"],
        key=f"edit_remarks_{reservation_id}",
    )

    custom_values = {}

    if fields:

        st.markdown(
            "#### 機器固有の入力項目"
        )

        for field in fields:

            custom_values[
                field["id"]
            ] = render_custom_field(
                field,
                f"edit_field_{reservation_id}",
                field_values.get(
                    field["id"]
                ),
            )

    col1, col2 = st.columns(2)

    with col1:

        if st.button(
            "変更を保存",
            type="primary",
            use_container_width=True,
            key=f"save_edit_{reservation_id}",
        ):

            errors = []

            start_dt = combine_datetime(
                start_date,
                start_time,
            )

            end_dt = combine_datetime(
                end_date,
                end_time,
            )

            if not user_name.strip():
                errors.append(
                    "氏名を入力してください。"
                )

            if not affiliation.strip():
                errors.append(
                    "所属講座を入力してください。"
                )

            if end_dt <= start_dt:
                errors.append(
                    "終了日時は開始日時より"
                    "後に設定してください。"
                )

            if end_dt <= datetime.now():
                errors.append(
                    "終了済みの日時へ変更することは"
                    "できません。"
                )

            if (
                purpose == "その他"
                and
                not purpose_other.strip()
            ):
                errors.append(
                    "「その他」の内容を"
                    "入力してください。"
                )

            for field in fields:

                if (
                    field["required"]
                    and
                    field_value_is_empty(
                        field,
                        custom_values[
                            field["id"]
                        ],
                    )
                ):

                    errors.append(
                        f"「{field['field_name']}」"
                        "を入力してください。"
                    )

            if errors:

                for error in errors:
                    st.error(error)

                return

            conflict, message = (
                reservation_has_conflict(
                    reservation[
                        "instrument_id"
                    ],
                    start_dt,
                    end_dt,
                    exclude_reservation_id=(
                        reservation_id
                    ),
                )
            )

            if conflict:

                st.error(message)

                return

            update_reservation(
                reservation_id=reservation_id,
                user_name=user_name,
                affiliation=affiliation,
                start_date=start_date,
                end_date=end_date,
                start_time=start_time,
                end_time=end_time,
                purpose=purpose,
                purpose_other=purpose_other,
                remarks=remarks,
                custom_values=custom_values,
            )

            write_local_storage(
                "equipment_booking_name",
                user_name.strip(),
            )

            write_local_storage(
                "equipment_booking_affiliation",
                affiliation.strip(),
            )

            st.session_state[
                "edit_reservation_id"
            ] = None

            st.success(
                "予約内容を変更しました。"
            )

            st.rerun()

    with col2:

        if st.button(
            "編集をやめる",
            use_container_width=True,
            key=f"cancel_edit_{reservation_id}",
        ):

            st.session_state[
                "edit_reservation_id"
            ] = None

            st.rerun()


# ============================================================
# Reservation detail / cancel
# ============================================================

def render_reservation_operations(
    reservations: list[sqlite3.Row],
) -> None:

    if not reservations:
        return

    st.divider()

    st.markdown(
        "### 予約の確認・編集・取消"
    )

    now = datetime.now()

    editable_reservations = [
        reservation
        for reservation in reservations
        if reservation_end_datetime(
            reservation
        ) > now
    ]

    if not editable_reservations:

        st.caption(
            "編集または取消可能な予約はありません。"
        )

        return

    reservation_map = {}

    for reservation in editable_reservations:

        label = (
            f"{format_reservation_period(reservation)}"
            f" ｜ {reservation['user_name']}"
        )

        reservation_map[
            label
        ] = reservation["id"]

    selected_label = st.selectbox(
        "予約を選択",
        list(
            reservation_map.keys()
        ),
        key="selected_reservation_operation",
    )

    reservation_id = reservation_map[
        selected_label
    ]

    reservation = get_reservation(
        reservation_id
    )

    if reservation is None:
        return

    with st.container(
        border=True
    ):

        st.write(
            f"**予約者：** "
            f"{reservation['user_name']}"
        )

        st.write(
            f"**所属講座：** "
            f"{reservation['affiliation']}"
        )

        st.write(
            f"**予約期間：** "
            f"{format_reservation_period(reservation)}"
        )

        st.write(
            f"**使用目的：** "
            f"{purpose_label(reservation)}"
        )

        pin = st.text_input(
            "4桁の暗証番号",
            type="password",
            max_chars=4,
            key=(
                f"operation_pin_"
                f"{reservation_id}"
            ),
        )

        col1, col2 = st.columns(2)

        with col1:

            if st.button(
                "予約を編集",
                type="primary",
                use_container_width=True,
                key=f"edit_button_{reservation_id}",
            ):

                if not verify_hash(
                    pin,
                    reservation["pin_salt"],
                    reservation["pin_hash"],
                ):

                    st.error(
                        "暗証番号が正しくありません。"
                    )

                else:

                    st.session_state[
                        "edit_reservation_id"
                    ] = reservation_id

                    st.rerun()

        with col2:

            if st.button(
                "予約を取り消す",
                use_container_width=True,
                key=f"delete_button_{reservation_id}",
            ):

                if not verify_hash(
                    pin,
                    reservation["pin_salt"],
                    reservation["pin_hash"],
                ):

                    st.error(
                        "暗証番号が正しくありません。"
                    )

                elif (
                    reservation_end_datetime(
                        reservation
                    )
                    <= datetime.now()
                ):

                    st.error(
                        "終了済みの予約は"
                        "取り消せません。"
                    )

                else:

                    delete_reservation(
                        reservation_id
                    )

                    st.success(
                        "予約を取り消しました。"
                    )

                    st.rerun()


# ============================================================
# Main booking page
# ============================================================

def page_booking(
    instrument_id: int,
) -> None:

    instrument = get_instrument(
        instrument_id
    )

    if instrument is None:
        return

    st.header(
        instrument["name"]
    )

    if instrument["notice"]:
        st.info(
            instrument["notice"]
        )

    col1, col2, col3 = st.columns(
        [1, 1, 2]
    )

    with col1:

        view_mode = st.radio(
            "表示",
            [
                "週間",
                "1日",
            ],
            horizontal=True,
        )

    with col2:

        selected_date = st.date_input(
            "表示日",
            value=date.today(),
            format="YYYY/MM/DD",
        )

    with col3:

        st.write("")

        st.write("")

        show_new_form = st.toggle(
            "新規予約フォームを表示",
            value=False,
        )

    if view_mode == "週間":

        start_date = get_week_start(
            selected_date
        )

        end_date = (
            start_date
            + timedelta(days=6)
        )

        dates = [
            start_date
            + timedelta(days=index)
            for index in range(7)
        ]

    else:

        start_date = selected_date
        end_date = selected_date
        dates = [
            selected_date
        ]

    reservations = (
        get_reservations_for_range(
            instrument_id,
            start_date,
            end_date,
        )
    )

    blocked_periods = (
        get_blocked_periods_for_range(
            instrument_id,
            start_date,
            end_date,
        )
    )

    render_calendar(
        dates,
        reservations,
        blocked_periods,
    )

    if (
        st.session_state[
            "edit_reservation_id"
        ]
        is not None
    ):

        st.divider()

        render_edit_reservation(
            st.session_state[
                "edit_reservation_id"
            ]
        )

    else:

        render_reservation_operations(
            reservations
        )

        if show_new_form:

            st.divider()

            render_new_reservation_form(
                instrument_id
            )

    st.divider()

    if st.button(
        "保存された利用者情報をクリア"
    ):

        clear_local_storage()

        for key in [
            "new_name",
            "new_affiliation",
        ]:

            st.session_state.pop(
                key,
                None,
            )

        st.success(
            "保存された利用者情報を"
            "クリアしました。"
        )

        st.rerun()


# ============================================================
# Manager login
# ============================================================

def render_login() -> bool:

    if st.session_state[
        "logged_in"
    ]:

        st.success(
            "ログイン中："
            f"{st.session_state['display_name']}"
        )

        if st.button(
            "ログアウト"
        ):

            logout()

        return True

    st.header(
        "管理者ログイン"
    )

    with st.form(
        "login_form"
    ):

        username = st.text_input(
            "ユーザー名"
        )

        password = st.text_input(
            "パスワード",
            type="password",
        )

        submitted = (
            st.form_submit_button(
                "ログイン",
                type="primary",
            )
        )

    if submitted:

        if authenticate(
            username,
            password,
        ):

            st.rerun()

        else:

            st.error(
                "ユーザー名または"
                "パスワードが正しくありません。"
            )

    return False


# ============================================================
# System administration
# ============================================================

def admin_instrument_management() -> None:

    st.subheader(
        "機器管理"
    )

    with st.form(
        "add_instrument"
    ):

        name = st.text_input(
            "機器名 *"
        )

        description = st.text_area(
            "説明"
        )

        notice = st.text_area(
            "利用者への注意事項"
        )

        submitted = (
            st.form_submit_button(
                "機器を追加"
            )
        )

    if submitted:

        if not name.strip():

            st.error(
                "機器名を入力してください。"
            )

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

                st.success(
                    "機器を追加しました。"
                )

                st.rerun()

            except sqlite3.IntegrityError:

                st.error(
                    "同名の機器が"
                    "既に登録されています。"
                )

    for instrument in get_instruments(
        active_only=False
    ):

        with st.expander(
            instrument["name"]
        ):

            new_name = st.text_input(
                "機器名",
                value=instrument["name"],
                key=(
                    f"admin_instrument_name_"
                    f"{instrument['id']}"
                ),
            )

            description = st.text_area(
                "説明",
                value=instrument["description"],
                key=(
                    f"admin_description_"
                    f"{instrument['id']}"
                ),
            )

            notice = st.text_area(
                "注意事項",
                value=instrument["notice"],
                key=(
                    f"admin_notice_"
                    f"{instrument['id']}"
                ),
            )

            active = st.checkbox(
                "予約可能",
                value=bool(
                    instrument["active"]
                ),
                key=(
                    f"admin_active_"
                    f"{instrument['id']}"
                ),
            )

            if st.button(
                "保存",
                key=(
                    f"admin_save_instrument_"
                    f"{instrument['id']}"
                ),
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

                    st.error(
                        "同名の機器が"
                        "既に登録されています。"
                    )

            st.markdown(
                "#### 機器削除"
            )

            confirm = st.checkbox(
                "予約情報を含め、この機器を削除する",
                key=(
                    f"admin_confirm_delete_"
                    f"{instrument['id']}"
                ),
            )

            if st.button(
                "機器を削除",
                disabled=not confirm,
                key=(
                    f"admin_delete_instrument_"
                    f"{instrument['id']}"
                ),
            ):

                delete_instrument(
                    instrument["id"]
                )

                st.rerun()


def admin_manager_management() -> None:

    st.subheader(
        "機器管理者"
    )

    with st.form(
        "add_manager"
    ):

        username = st.text_input(
            "ユーザー名 *"
        )

        display_name = st.text_input(
            "表示名 *"
        )

        password = st.text_input(
            "初期パスワード *",
            type="password",
        )

        submitted = (
            st.form_submit_button(
                "機器管理者を追加"
            )
        )

    if submitted:

        if (
            not username.strip()
            or
            not display_name.strip()
            or
            not password
        ):

            st.error(
                "すべての必須項目を"
                "入力してください。"
            )

        else:

            salt, password_hash = (
                make_hash(
                    password
                )
            )

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

                st.error(
                    "同じユーザー名が"
                    "既に存在します。"
                )

    with get_connection() as conn:

        managers = conn.execute(
            """
            SELECT *
            FROM manager_accounts
            ORDER BY display_name
            """
        ).fetchall()

    instruments = get_instruments(
        active_only=False
    )

    if managers and instruments:

        st.markdown(
            "### 担当機器の割り当て"
        )

        manager_map = {
            (
                f"{row['display_name']}"
                f"（{row['username']}）"
            ):
            row["id"]
            for row in managers
            if row["active"]
        }

        instrument_map = {
            row["name"]: row["id"]
            for row in instruments
        }

        with st.form(
            "assign_instrument"
        ):

            selected_manager = st.selectbox(
                "機器管理者",
                list(
                    manager_map.keys()
                ),
            )

            selected_instrument = st.selectbox(
                "機器",
                list(
                    instrument_map.keys()
                ),
            )

            submitted = (
                st.form_submit_button(
                    "担当機器を割り当て"
                )
            )

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
                            instrument_map[
                                selected_instrument
                            ],
                            manager_map[
                                selected_manager
                            ],
                        ),
                    )

                st.rerun()

            except sqlite3.IntegrityError:

                st.error(
                    "既に割り当て済みです。"
                )


# ============================================================
# Instrument manager settings
# ============================================================

def custom_field_management(
    instrument_id: int,
) -> None:

    st.markdown(
        "### 予約入力項目"
    )

    type_labels = {
        "一行テキスト": "text",
        "複数行テキスト": "textarea",
        "単一選択": "select",
        "複数選択": "multiselect",
        "数値": "number",
        "チェックボックス": "checkbox",
    }

    with st.form(
        f"custom_field_{instrument_id}"
    ):

        field_name = st.text_input(
            "項目名 *"
        )

        type_label = st.selectbox(
            "入力形式",
            list(
                type_labels.keys()
            ),
        )

        required = st.checkbox(
            "必須項目にする"
        )

        options_text = st.text_area(
            "選択肢",
            help=(
                "選択形式の場合、"
                "1行に1項目入力してください。"
            ),
        )

        submitted = (
            st.form_submit_button(
                "入力項目を追加"
            )
        )

    if submitted:

        field_type = type_labels[
            type_label
        ]

        options = [
            line.strip()
            for line
            in options_text.splitlines()
            if line.strip()
        ]

        if not field_name.strip():

            st.error(
                "項目名を入力してください。"
            )

        elif (
            field_type
            in {
                "select",
                "multiselect",
            }
            and
            not options
        ):

            st.error(
                "選択肢を入力してください。"
            )

        else:

            with get_connection() as conn:

                row = conn.execute(
                    """
                    SELECT
                        COALESCE(
                            MAX(display_order),
                            0
                        ) AS max_order
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
                        json.dumps(
                            options,
                            ensure_ascii=False,
                        ),
                        row["max_order"] + 1,
                    ),
                )

            st.rerun()

    fields = get_custom_fields(
        instrument_id
    )

    for field in fields:

        col1, col2 = st.columns(
            [6, 1]
        )

        with col1:

            st.write(
                f"**{field['field_name']}**"
                f" ｜ "
                f"{'必須' if field['required'] else '任意'}"
            )

        with col2:

            if st.button(
                "削除",
                key=(
                    f"delete_custom_field_"
                    f"{field['id']}"
                ),
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


def blocked_period_management(
    instrument_id: int,
) -> None:

    st.markdown(
        "### 使用停止期間"
    )

    with st.form(
        f"blocked_{instrument_id}"
    ):

        blocked_date = st.date_input(
            "日付",
            value=date.today(),
            format="YYYY/MM/DD",
        )

        col1, col2 = st.columns(2)

        with col1:

            start_time = st.selectbox(
                "開始時刻",
                TIME_OPTIONS,
                index=36,
            )

        with col2:

            end_time = st.selectbox(
                "終了時刻",
                TIME_OPTIONS,
                index=68,
            )

        reason = st.text_input(
            "理由"
        )

        submitted = (
            st.form_submit_button(
                "使用停止期間を追加"
            )
        )

    if submitted:

        start_dt = combine_datetime(
            blocked_date,
            start_time,
        )

        end_dt = combine_datetime(
            blocked_date,
            end_time,
        )

        if end_dt <= start_dt:

            st.error(
                "終了時刻は開始時刻より"
                "後にしてください。"
            )

        else:

            conflict, message = (
                reservation_has_conflict(
                    instrument_id,
                    start_dt,
                    end_dt,
                )
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
                            start_time,
                            end_time,
                            reason.strip(),
                        ),
                    )

                st.rerun()


def manager_instrument_settings() -> None:

    instrument_ids = (
        manageable_instrument_ids()
    )

    instruments = [
        instrument
        for instrument in (
            get_instrument(
                instrument_id
            )
            for instrument_id
            in instrument_ids
        )
        if instrument is not None
    ]

    if not instruments:

        st.info(
            "担当機器はありません。"
        )

        return

    instrument_map = {
        row["name"]: row["id"]
        for row in instruments
    }

    selected_name = st.selectbox(
        "管理する機器",
        list(
            instrument_map.keys()
        ),
    )

    instrument_id = instrument_map[
        selected_name
    ]

    instrument = get_instrument(
        instrument_id
    )

    if instrument is None:
        return

    st.markdown(
        "### 基本設定"
    )

    description = st.text_area(
        "機器の説明",
        value=instrument["description"],
    )

    notice = st.text_area(
        "利用者への注意事項",
        value=instrument["notice"],
    )

    if st.button(
        "基本設定を保存"
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

    custom_field_management(
        instrument_id
    )

    st.divider()

    blocked_period_management(
        instrument_id
    )

    st.divider()

    st.markdown(
        "### 機器の削除"
    )

    st.warning(
        "機器を削除すると、予約情報、"
        "入力項目、使用停止期間も削除されます。"
    )

    confirm = st.checkbox(
        f"「{instrument['name']}」を削除する",
        key=f"manager_delete_confirm_{instrument_id}",
    )

    if st.button(
        "担当機器をシステムから削除",
        disabled=not confirm,
        key=f"manager_delete_instrument_{instrument_id}",
    ):

        delete_instrument(
            instrument_id
        )

        st.rerun()


def manager_reservation_management() -> None:

    instrument_ids = (
        manageable_instrument_ids()
    )

    instruments = [
        instrument
        for instrument in (
            get_instrument(
                instrument_id
            )
            for instrument_id
            in instrument_ids
        )
        if instrument is not None
    ]

    if not instruments:

        st.info(
            "管理できる機器はありません。"
        )

        return

    instrument_map = {
        row["name"]: row["id"]
        for row in instruments
    }

    selected_name = st.selectbox(
        "機器",
        list(
            instrument_map.keys()
        ),
        key="manager_reservation_instrument",
    )

    reservations = get_reservations(
        instrument_map[
            selected_name
        ]
    )

    for reservation in reservations:

        with st.expander(
            (
                f"{format_reservation_period(reservation)}"
                f" ｜ {reservation['user_name']}"
            )
        ):

            st.write(
                f"**所属講座：** "
                f"{reservation['affiliation']}"
            )

            st.write(
                f"**使用目的：** "
                f"{purpose_label(reservation)}"
            )

            if reservation["remarks"]:

                st.write(
                    f"**備考：** "
                    f"{reservation['remarks']}"
                )

            for field_value in (
                get_reservation_field_values(
                    reservation["id"]
                )
            ):

                value = json.loads(
                    field_value["value_json"]
                )

                if isinstance(value, list):

                    display_value = (
                        "、".join(
                            map(str, value)
                        )
                    )

                elif isinstance(value, bool):

                    display_value = (
                        "はい"
                        if value
                        else "いいえ"
                    )

                else:

                    display_value = str(
                        value
                    )

                st.write(
                    f"**"
                    f"{field_value['field_name_snapshot']}"
                    f"：** "
                    f"{display_value}"
                )

            if st.button(
                "管理者権限で予約を削除",
                key=(
                    f"manager_delete_reservation_"
                    f"{reservation['id']}"
                ),
            ):

                delete_reservation(
                    reservation["id"]
                )

                st.rerun()


# ============================================================
# Management page
# ============================================================

def page_management() -> None:

    if not render_login():
        return

    if (
        st.session_state["role"]
        == "system_admin"
    ):

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
# Main
# ============================================================

def main() -> None:

    init_db()
    init_session()

    st.title(
        APP_TITLE
    )

    page = st.sidebar.radio(
        "メニュー",
        [
            "予約・予約状況",
            "管理者",
        ],
    )

    if page == "管理者":

        page_management()

        return

    instruments = get_instruments(
        active_only=True
    )

    if not instruments:

        st.info(
            "現在、予約可能な機器は"
            "登録されていません。"
        )

        return

    st.sidebar.divider()

    st.sidebar.subheader(
        "機器選択"
    )

    instrument_ids = [
        instrument["id"]
        for instrument in instruments
    ]

    last_instrument = read_local_storage(
        "equipment_booking_last_instrument"
    )

    default_index = 0

    try:

        last_instrument_id = int(
            last_instrument
        )

        if (
            last_instrument_id
            in instrument_ids
        ):

            default_index = (
                instrument_ids.index(
                    last_instrument_id
                )
            )

    except (
        TypeError,
        ValueError,
    ):
        pass

    instrument_name_map = {
        instrument["name"]:
            instrument["id"]
        for instrument in instruments
    }

    selected_name = st.sidebar.radio(
        "予約する機器",
        list(
            instrument_name_map.keys()
        ),
        index=default_index,
        label_visibility="collapsed",
    )

    selected_instrument_id = (
        instrument_name_map[
            selected_name
        ]
    )

    page_booking(
        selected_instrument_id
    )


if __name__ == "__main__":

    main()
