import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
import streamlit as st


APP_TITLE = "実験機器 使用予約システム"
DB_PATH = "equipment_booking.db"

SYSTEM_ADMIN_USERNAME = "admin"
SYSTEM_ADMIN_PASSWORD = "1234"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧪",
    layout="wide",
)


# ============================================================
# Database
# ============================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
                reservation_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                purpose TEXT NOT NULL,
                purpose_other TEXT NOT NULL DEFAULT '',
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


# ============================================================
# Security helpers
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


def verify_hash(
    value: str,
    salt_hex: str,
    stored_hash: str,
) -> bool:
    _, calculated = make_hash(value, salt_hex)

    return hmac.compare_digest(
        calculated,
        stored_hash,
    )


# ============================================================
# Time helpers
# ============================================================

def generate_time_options() -> list[str]:
    current = datetime.combine(
        date.today(),
        time(0, 0),
    )

    result: list[str] = []

    for _ in range(96):
        result.append(
            current.strftime("%H:%M")
        )

        current += timedelta(minutes=15)

    return result


TIME_OPTIONS = generate_time_options()


def to_minutes(value: str) -> int:
    hour, minute = map(
        int,
        value.split(":"),
    )

    return hour * 60 + minute


def overlaps(
    start_a: str,
    end_a: str,
    start_b: str,
    end_b: str,
) -> bool:
    return (
        to_minutes(start_a) < to_minutes(end_b)
        and
        to_minutes(end_a) > to_minutes(start_b)
    )


# ============================================================
# Data access
# ============================================================

def get_instruments(
    active_only: bool = True,
) -> list[sqlite3.Row]:

    query = "SELECT * FROM instruments"

    if active_only:
        query += " WHERE active = 1"

    query += " ORDER BY name"

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


def get_reservations(
    instrument_id: int,
    target_date: date | None = None,
) -> list[sqlite3.Row]:

    query = """
        SELECT
            r.*,
            i.name AS instrument_name
        FROM reservations r
        JOIN instruments i
            ON i.id = r.instrument_id
        WHERE r.instrument_id = ?
    """

    params: list[Any] = [
        instrument_id
    ]

    if target_date is not None:
        query += """
            AND r.reservation_date = ?
        """

        params.append(
            target_date.isoformat()
        )

    query += """
        ORDER BY
            r.reservation_date,
            r.start_time,
            r.end_time
    """

    with get_connection() as conn:
        return conn.execute(
            query,
            params,
        ).fetchall()


def get_blocked_periods(
    instrument_id: int,
    target_date: date | None = None,
) -> list[sqlite3.Row]:

    query = """
        SELECT *
        FROM blocked_periods
        WHERE instrument_id = ?
    """

    params: list[Any] = [
        instrument_id
    ]

    if target_date is not None:
        query += """
            AND reservation_date = ?
        """

        params.append(
            target_date.isoformat()
        )

    query += """
        ORDER BY
            reservation_date,
            start_time
    """

    with get_connection() as conn:
        return conn.execute(
            query,
            params,
        ).fetchall()


def reservation_has_conflict(
    instrument_id: int,
    reservation_date: date,
    start_time: str,
    end_time: str,
) -> tuple[bool, str]:

    reservations = get_reservations(
        instrument_id,
        reservation_date,
    )

    for reservation in reservations:

        if overlaps(
            start_time,
            end_time,
            reservation["start_time"],
            reservation["end_time"],
        ):
            return (
                True,
                "指定した時間帯には既に予約があります。",
            )

    blocked_periods = get_blocked_periods(
        instrument_id,
        reservation_date,
    )

    for blocked in blocked_periods:

        if overlaps(
            start_time,
            end_time,
            blocked["start_time"],
            blocked["end_time"],
        ):
            reason = blocked[
                "reason"
            ].strip()

            message = (
                "指定した時間帯は"
                "使用停止期間です。"
            )

            if reason:
                message += (
                    f" 理由：{reason}"
                )

            return True, message

    return False, ""


def add_reservation(
    instrument_id: int,
    user_name: str,
    affiliation: str,
    reservation_date: date,
    start_time: str,
    end_time: str,
    purpose: str,
    purpose_other: str,
    pin: str,
    custom_values: dict[int, Any],
) -> None:

    pin_salt, pin_hash = make_hash(
        pin
    )

    fields = {
        field["id"]: field
        for field
        in get_custom_fields(
            instrument_id
        )
    }

    with get_connection() as conn:

        cursor = conn.execute(
            """
            INSERT INTO reservations (
                instrument_id,
                user_name,
                affiliation,
                reservation_date,
                start_time,
                end_time,
                purpose,
                purpose_other,
                pin_salt,
                pin_hash
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                instrument_id,
                user_name.strip(),
                affiliation.strip(),
                reservation_date.isoformat(),
                start_time,
                end_time,
                purpose,
                purpose_other.strip(),
                pin_salt,
                pin_hash,
            ),
        )

        reservation_id = (
            cursor.lastrowid
        )

        for (
            field_id,
            value,
        ) in custom_values.items():

            field = fields.get(
                field_id
            )

            if field is None:
                continue

            conn.execute(
                """
                INSERT INTO
                reservation_field_values (
                    reservation_id,
                    custom_field_id,
                    field_name_snapshot,
                    field_type_snapshot,
                    value_json
                )
                VALUES (
                    ?, ?, ?, ?, ?
                )
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


# ============================================================
# Login / authorization
# ============================================================

def init_session() -> None:

    st.session_state.setdefault(
        "logged_in",
        False,
    )

    st.session_state.setdefault(
        "role",
        None,
    )

    st.session_state.setdefault(
        "manager_id",
        None,
    )

    st.session_state.setdefault(
        "username",
        None,
    )

    st.session_state.setdefault(
        "display_name",
        None,
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
            "manager_id"
        ] = None

        st.session_state[
            "username"
        ] = username

        st.session_state[
            "display_name"
        ] = "システム管理者"

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
            for row
            in get_instruments(
                active_only=False
            )
        ]

    manager_id = (
        st.session_state[
            "manager_id"
        ]
    )

    if manager_id is None:
        return []

    return get_managed_instrument_ids(
        manager_id
    )


# ============================================================
# Display helpers
# ============================================================

def purpose_label(
    row: sqlite3.Row,
) -> str:

    if row["purpose"] == "その他":

        detail = row[
            "purpose_other"
        ].strip()

        if detail:
            return (
                f"その他（{detail}）"
            )

        return "その他"

    return row["purpose"]


def make_schedule_dataframe(
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
) -> pd.DataFrame:

    rows: list[
        dict[str, str]
    ] = []

    for reservation in reservations:

        rows.append(
            {
                "開始":
                    reservation["start_time"],
                "終了":
                    reservation["end_time"],
                "予約者":
                    reservation["user_name"],
                "所属":
                    reservation["affiliation"],
                "使用目的":
                    purpose_label(
                        reservation
                    ),
                "状態":
                    "予約",
            }
        )

    for blocked in blocked_periods:

        rows.append(
            {
                "開始":
                    blocked["start_time"],
                "終了":
                    blocked["end_time"],
                "予約者":
                    "",
                "所属":
                    "",
                "使用目的":
                    blocked["reason"],
                "状態":
                    "使用停止",
            }
        )

    if not rows:

        return pd.DataFrame(
            columns=[
                "開始",
                "終了",
                "予約者",
                "所属",
                "使用目的",
                "状態",
            ]
        )

    return (
        pd.DataFrame(rows)
        .sort_values(
            [
                "開始",
                "終了",
            ]
        )
        .reset_index(
            drop=True
        )
    )


def render_custom_field(
    field: sqlite3.Row,
    key_prefix: str,
) -> Any:

    label = field[
        "field_name"
    ]

    if field["required"]:
        label += " *"

    key = (
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
            key=key,
        )

    if field_type == "textarea":

        return st.text_area(
            label,
            key=key,
        )

    if field_type == "select":

        selected = st.selectbox(
            label,
            [
                "選択してください"
            ]
            + options,
            key=key,
        )

        if (
            selected
            == "選択してください"
        ):
            return ""

        return selected

    if field_type == "multiselect":

        return st.multiselect(
            label,
            options,
            key=key,
        )

    if field_type == "number":

        return st.number_input(
            label,
            min_value=0.0,
            step=1.0,
            key=key,
        )

    if field_type == "checkbox":

        return st.checkbox(
            label,
            key=key,
        )

    return st.text_input(
        label,
        key=key,
    )


def custom_value_is_empty(
    field: sqlite3.Row,
    value: Any,
) -> bool:

    if (
        field["field_type"]
        == "checkbox"
    ):
        return value is False

    if (
        field["field_type"]
        == "multiselect"
    ):
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


# ============================================================
# User pages
# ============================================================

def page_schedule() -> None:

    st.header("予約状況")

    instruments = get_instruments(
        active_only=True
    )

    if not instruments:

        st.info(
            "現在、予約可能な機器は"
            "登録されていません。"
        )

        return

    instrument_map = {
        row["name"]: row["id"]
        for row in instruments
    }

    col1, col2 = st.columns(2)

    with col1:

        selected_name = st.selectbox(
            "機器",
            list(
                instrument_map.keys()
            ),
        )

    with col2:

        selected_date = st.date_input(
            "使用日",
            value=date.today(),
            format="YYYY/MM/DD",
        )

    instrument_id = (
        instrument_map[
            selected_name
        ]
    )

    instrument = get_instrument(
        instrument_id
    )

    if (
        instrument
        and
        instrument[
            "notice"
        ].strip()
    ):

        st.info(
            instrument["notice"]
        )

    df = make_schedule_dataframe(
        get_reservations(
            instrument_id,
            selected_date,
        ),
        get_blocked_periods(
            instrument_id,
            selected_date,
        ),
    )

    if df.empty:

        st.success(
            "この日の予約はありません。"
        )

    else:

        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
        )


def page_new_reservation() -> None:

    st.header("新規予約")

    instruments = get_instruments(
        active_only=True
    )

    if not instruments:

        st.info(
            "現在、予約可能な機器は"
            "登録されていません。"
        )

        return

    instrument_map = {
        row["name"]: row["id"]
        for row in instruments
    }

    selected_name = st.selectbox(
        "機器 *",
        list(
            instrument_map.keys()
        ),
    )

    instrument_id = (
        instrument_map[
            selected_name
        ]
    )

    instrument = get_instrument(
        instrument_id
    )

    if instrument:

        if instrument[
            "description"
        ].strip():

            st.caption(
                instrument[
                    "description"
                ]
            )

        if instrument[
            "notice"
        ].strip():

            st.info(
                instrument["notice"]
            )

    fields = get_custom_fields(
        instrument_id
    )

    with st.form(
        f"reservation_form_"
        f"{instrument_id}"
    ):

        user_name = st.text_input(
            "氏名 *"
        )

        affiliation = st.text_input(
            "所属 *"
        )

        reservation_date = (
            st.date_input(
                "使用日 *",
                value=date.today(),
                min_value=date.today(),
                format="YYYY/MM/DD",
            )
        )

        col1, col2 = st.columns(2)

        with col1:

            start_time = st.selectbox(
                "開始時刻 *",
                TIME_OPTIONS,
                index=36,
            )

        with col2:

            end_time = st.selectbox(
                "終了時刻 *",
                TIME_OPTIONS,
                index=40,
            )

        purpose = st.selectbox(
            "使用目的 *",
            [
                "測定",
                "解析のみ",
                "その他",
            ],
        )

        purpose_other = ""

        if purpose == "その他":

            purpose_other = (
                st.text_input(
                    "「その他」の内容 *"
                )
            )

        custom_values: dict[
            int,
            Any,
        ] = {}

        if fields:

            st.subheader(
                "機器固有の入力項目"
            )

            for field in fields:

                custom_values[
                    field["id"]
                ] = render_custom_field(
                    field,
                    (
                        f"reservation_"
                        f"{instrument_id}"
                    ),
                )

        pin = st.text_input(
            "4桁の暗証番号 *",
            type="password",
            max_chars=4,
            help=(
                "予約を取り消す際に"
                "必要です。"
            ),
        )

        submitted = (
            st.form_submit_button(
                "予約する",
                type="primary",
                use_container_width=True,
            )
        )

    if not submitted:
        return

    errors: list[str] = []

    if not user_name.strip():

        errors.append(
            "氏名を入力してください。"
        )

    if not affiliation.strip():

        errors.append(
            "所属を入力してください。"
        )

    if (
        to_minutes(start_time)
        >=
        to_minutes(end_time)
    ):

        errors.append(
            "終了時刻は開始時刻より"
            "後を選択してください。"
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
            custom_value_is_empty(
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
            reservation_date,
            start_time,
            end_time,
        )
    )

    if conflict:

        st.error(message)

        return

    add_reservation(
        instrument_id=instrument_id,
        user_name=user_name,
        affiliation=affiliation,
        reservation_date=reservation_date,
        start_time=start_time,
        end_time=end_time,
        purpose=purpose,
        purpose_other=purpose_other,
        pin=pin,
        custom_values=custom_values,
    )

    st.success(
        f"{selected_name}を "
        f"{reservation_date.strftime('%Y年%m月%d日')} "
        f"{start_time}〜{end_time} "
        "で予約しました。"
    )


def page_cancel_reservation() -> None:

    st.header("予約取消")

    instruments = get_instruments(
        active_only=True
    )

    if not instruments:

        st.info(
            "予約可能な機器は"
            "登録されていません。"
        )

        return

    instrument_map = {
        row["name"]: row["id"]
        for row in instruments
    }

    col1, col2 = st.columns(2)

    with col1:

        selected_name = st.selectbox(
            "機器",
            list(
                instrument_map.keys()
            ),
            key="cancel_instrument",
        )

    with col2:

        selected_date = st.date_input(
            "使用日",
            value=date.today(),
            format="YYYY/MM/DD",
            key="cancel_date",
        )

    reservations = get_reservations(
        instrument_map[
            selected_name
        ],
        selected_date,
    )

    if not reservations:

        st.info(
            "この日の予約はありません。"
        )

        return

    reservation_map: dict[
        str,
        int,
    ] = {}

    for row in reservations:

        label = (
            f"{row['start_time']}"
            f"〜{row['end_time']} ｜ "
            f"{row['user_name']} ｜ "
            f"{row['affiliation']}"
        )

        reservation_map[
            label
        ] = row["id"]

    selected_label = st.selectbox(
        "取り消す予約",
        list(
            reservation_map.keys()
        ),
    )

    reservation = get_reservation(
        reservation_map[
            selected_label
        ]
    )

    if reservation is None:

        st.error(
            "予約情報を取得できませんでした。"
        )

        return

    st.write(
        f"**使用目的：** "
        f"{purpose_label(reservation)}"
    )

    pin = st.text_input(
        "4桁の暗証番号",
        type="password",
        max_chars=4,
    )

    if st.button(
        "予約を取り消す",
        type="primary",
        use_container_width=True,
    ):

        if not verify_hash(
            pin,
            reservation["pin_salt"],
            reservation["pin_hash"],
        ):

            st.error(
                "暗証番号が正しくありません。"
            )

            return

        delete_reservation(
            reservation["id"]
        )

        st.success(
            "予約を取り消しました。"
        )

        st.rerun()


# ============================================================
# Admin / manager pages
# ============================================================

def render_login() -> bool:

    if st.session_state[
        "logged_in"
    ]:

        st.success(
            "ログイン中："
            f"{st.session_state['display_name']} "
            f"（{st.session_state['username']}）"
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

            st.success(
                "ログインしました。"
            )

            st.rerun()

        else:

            st.error(
                "ユーザー名または"
                "パスワードが正しくありません。"
            )

    return False


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


def admin_instrument_management() -> None:

    st.subheader(
        "機器管理"
    )

    with st.form(
        "add_instrument_form"
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
                    "同じ名前の機器が"
                    "既に登録されています。"
                )

    instruments = get_instruments(
        active_only=False
    )

    for instrument in instruments:

        with st.expander(
            instrument["name"]
        ):

            new_name = st.text_input(
                "機器名",
                value=instrument["name"],
                key=(
                    f"instrument_name_"
                    f"{instrument['id']}"
                ),
            )

            new_description = (
                st.text_area(
                    "説明",
                    value=instrument[
                        "description"
                    ],
                    key=(
                        f"instrument_description_"
                        f"{instrument['id']}"
                    ),
                )
            )

            new_notice = st.text_area(
                "利用者への注意事項",
                value=instrument[
                    "notice"
                ],
                key=(
                    f"instrument_notice_"
                    f"{instrument['id']}"
                ),
            )

            active = st.checkbox(
                "予約可能",
                value=bool(
                    instrument["active"]
                ),
                key=(
                    f"instrument_active_"
                    f"{instrument['id']}"
                ),
            )

            if st.button(
                "保存",
                key=(
                    f"save_instrument_"
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
                                new_description.strip(),
                                new_notice.strip(),
                                int(active),
                                instrument["id"],
                            ),
                        )

                    st.success(
                        "保存しました。"
                    )

                    st.rerun()

                except sqlite3.IntegrityError:

                    st.error(
                        "同じ名前の機器が"
                        "既に登録されています。"
                    )


def admin_manager_management() -> None:

    st.subheader(
        "機器管理者"
    )

    with st.form(
        "add_manager_account"
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

        errors = []

        if not username.strip():

            errors.append(
                "ユーザー名を入力してください。"
            )

        if not display_name.strip():

            errors.append(
                "表示名を入力してください。"
            )

        if not password:

            errors.append(
                "初期パスワードを"
                "入力してください。"
            )

        if errors:

            for error in errors:
                st.error(error)

        else:

            salt, password_hash = (
                make_hash(password)
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

                st.success(
                    "機器管理者を"
                    "追加しました。"
                )

                st.rerun()

            except sqlite3.IntegrityError:

                st.error(
                    "同じユーザー名が"
                    "既に登録されています。"
                )

    with get_connection() as conn:

        managers = conn.execute(
            """
            SELECT *
            FROM manager_accounts
            ORDER BY username
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
            "assign_manager_form"
        ):

            selected_manager = (
                st.selectbox(
                    "機器管理者",
                    list(
                        manager_map.keys()
                    ),
                )
            )

            selected_instrument = (
                st.selectbox(
                    "機器",
                    list(
                        instrument_map.keys()
                    ),
                )
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

                st.success(
                    "担当機器を"
                    "割り当てました。"
                )

                st.rerun()

            except sqlite3.IntegrityError:

                st.error(
                    "この組み合わせは"
                    "既に登録されています。"
                )

    st.markdown(
        "### 登録済み機器管理者"
    )

    for manager in managers:

        with st.expander(
            (
                f"{manager['display_name']}"
                f"（{manager['username']}）"
            )
        ):

            with get_connection() as conn:

                assigned = conn.execute(
                    """
                    SELECT
                        im.id AS assignment_id,
                        i.name AS instrument_name
                    FROM instrument_managers im
                    JOIN instruments i
                        ON i.id = im.instrument_id
                    WHERE im.manager_id = ?
                    ORDER BY i.name
                    """,
                    (manager["id"],),
                ).fetchall()

            if assigned:

                for row in assigned:

                    col1, col2 = (
                        st.columns(
                            [5, 1]
                        )
                    )

                    with col1:

                        st.write(
                            row[
                                "instrument_name"
                            ]
                        )

                    with col2:

                        if st.button(
                            "解除",
                            key=(
                                f"remove_assignment_"
                                f"{row['assignment_id']}"
                            ),
                        ):

                            with get_connection() as conn:

                                conn.execute(
                                    """
                                    DELETE FROM instrument_managers
                                    WHERE id = ?
                                    """,
                                    (
                                        row[
                                            "assignment_id"
                                        ],
                                    ),
                                )

                            st.rerun()

            else:

                st.caption(
                    "担当機器はありません。"
                )

            active = st.checkbox(
                "アカウントを有効にする",
                value=bool(
                    manager["active"]
                ),
                key=(
                    f"manager_active_"
                    f"{manager['id']}"
                ),
            )

            new_password = st.text_input(
                "新しいパスワード",
                type="password",
                key=(
                    f"manager_password_"
                    f"{manager['id']}"
                ),
                help=(
                    "変更しない場合は"
                    "空欄のままにしてください。"
                ),
            )

            if st.button(
                "アカウント設定を保存",
                key=(
                    f"save_manager_"
                    f"{manager['id']}"
                ),
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

                        salt, password_hash = (
                            make_hash(
                                new_password
                            )
                        )

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

                st.success(
                    "保存しました。"
                )

                st.rerun()


def manager_instrument_settings() -> None:

    st.subheader(
        "担当機器設定"
    )

    ids = manageable_instrument_ids()

    instruments = [
        row
        for row in (
            get_instrument(i)
            for i in ids
        )
        if row is not None
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

    instrument_id = (
        instrument_map[
            selected_name
        ]
    )

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
        value=instrument[
            "description"
        ],
        key=(
            f"manager_description_"
            f"{instrument_id}"
        ),
    )

    notice = st.text_area(
        "利用者への注意事項",
        value=instrument[
            "notice"
        ],
        key=(
            f"manager_notice_"
            f"{instrument_id}"
        ),
    )

    if st.button(
        "基本設定を保存",
        key=(
            f"save_manager_settings_"
            f"{instrument_id}"
        ),
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

        st.success(
            "保存しました。"
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


def custom_field_management(
    instrument_id: int,
) -> None:

    st.markdown(
        "### 予約入力項目"
    )

    type_labels = {
        "一行テキスト":
            "text",
        "複数行テキスト":
            "textarea",
        "単一選択":
            "select",
        "複数選択":
            "multiselect",
        "数値":
            "number",
        "チェックボックス":
            "checkbox",
    }

    with st.form(
        f"add_custom_field_"
        f"{instrument_id}"
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
                "単一選択・複数選択の場合は"
                "1行に1つ入力してください。"
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

        errors = []

        if not field_name.strip():

            errors.append(
                "項目名を入力してください。"
            )

        if (
            field_type
            in {
                "select",
                "multiselect",
            }
            and
            not options
        ):

            errors.append(
                "選択肢を入力してください。"
            )

        if errors:

            for error in errors:
                st.error(error)

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

            st.success(
                "入力項目を追加しました。"
            )

            st.rerun()

    fields = get_custom_fields(
        instrument_id
    )

    if not fields:

        st.caption(
            "追加の入力項目はありません。"
        )

        return

    type_name_map = {
        value: key
        for key, value
        in type_labels.items()
    }

    for field in fields:

        col1, col2, col3, col4 = (
            st.columns(
                [4, 2, 1, 1]
            )
        )

        with col1:

            st.write(
                f"**{field['field_name']}**"
            )

            options = json.loads(
                field["options_json"]
                or "[]"
            )

            if options:

                st.caption(
                    " / ".join(options)
                )

        with col2:

            st.write(
                type_name_map.get(
                    field["field_type"],
                    "",
                )
            )

        with col3:

            st.write(
                "必須"
                if field["required"]
                else "任意"
            )

        with col4:

            if st.button(
                "削除",
                key=(
                    f"delete_field_"
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
        f"blocked_period_form_"
        f"{instrument_id}"
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
                key=(
                    f"blocked_start_"
                    f"{instrument_id}"
                ),
            )

        with col2:

            end_time = st.selectbox(
                "終了時刻",
                TIME_OPTIONS,
                index=68,
                key=(
                    f"blocked_end_"
                    f"{instrument_id}"
                ),
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

        if (
            to_minutes(start_time)
            >=
            to_minutes(end_time)
        ):

            st.error(
                "終了時刻は開始時刻より"
                "後を選択してください。"
            )

        else:

            conflict, _ = (
                reservation_has_conflict(
                    instrument_id,
                    blocked_date,
                    start_time,
                    end_time,
                )
            )

            if conflict:

                st.error(
                    "この時間帯には予約または"
                    "使用停止期間があります。"
                )

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

                st.success(
                    "使用停止期間を追加しました。"
                )

                st.rerun()

    blocked_periods = (
        get_blocked_periods(
            instrument_id
        )
    )

    for blocked in blocked_periods:

        col1, col2 = st.columns(
            [6, 1]
        )

        with col1:

            text = (
                f"{blocked['reservation_date']} ｜ "
                f"{blocked['start_time']}"
                f"〜{blocked['end_time']}"
            )

            if blocked["reason"]:

                text += (
                    f" ｜ {blocked['reason']}"
                )

            st.write(text)

        with col2:

            if st.button(
                "削除",
                key=(
                    f"delete_blocked_"
                    f"{blocked['id']}"
                ),
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


def manager_reservation_management() -> None:

    st.subheader(
        "予約管理"
    )

    ids = manageable_instrument_ids()

    instruments = [
        row
        for row in (
            get_instrument(i)
            for i in ids
        )
        if row is not None
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
        key=(
            "manager_reservation_instrument"
        ),
    )

    reservations = get_reservations(
        instrument_map[
            selected_name
        ]
    )

    if not reservations:

        st.info(
            "予約はありません。"
        )

        return

    for reservation in reservations:

        title = (
            f"{reservation['reservation_date']} "
            f"{reservation['start_time']}"
            f"〜{reservation['end_time']} ｜ "
            f"{reservation['user_name']}"
        )

        with st.expander(title):

            st.write(
                f"**所属：** "
                f"{reservation['affiliation']}"
            )

            st.write(
                f"**使用目的：** "
                f"{purpose_label(reservation)}"
            )

            field_values = (
                get_reservation_field_values(
                    reservation["id"]
                )
            )

            for field_value in field_values:

                value = json.loads(
                    field_value[
                        "value_json"
                    ]
                )

                if isinstance(
                    value,
                    list,
                ):

                    display_value = (
                        "、".join(
                            map(
                                str,
                                value,
                            )
                        )
                    )

                elif isinstance(
                    value,
                    bool,
                ):

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
                    f"manager_delete_"
                    f"{reservation['id']}"
                ),
            ):

                delete_reservation(
                    reservation["id"]
                )

                st.success(
                    "予約を削除しました。"
                )

                st.rerun()


# ============================================================
# Main
# ============================================================

def main() -> None:

    init_db()

    init_session()

    st.title(
        APP_TITLE
    )

    st.caption(
        "Equipment Booking System"
    )

    page = st.sidebar.radio(
        "メニュー",
        [
            "予約状況",
            "新規予約",
            "予約取消",
            "管理者",
        ],
    )

    if page == "予約状況":

        page_schedule()

    elif page == "新規予約":

        page_new_reservation()

    elif page == "予約取消":

        page_cancel_reservation()

    elif page == "管理者":

        page_management()


if __name__ == "__main__":

    main()
