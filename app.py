import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
import streamlit as st


# ============================================================
# Basic settings
# ============================================================

APP_TITLE = "実験機器 使用予約システム"
DB_PATH = "equipment_booking.db"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧪",
    layout="wide",
)

SYSTEM_ADMIN_EMAILS = {
    email.strip().lower()
    for email in st.secrets.get("system_admins", {}).get("emails", [])
}


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

            CREATE TABLE IF NOT EXISTS instrument_managers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                manager_email TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(instrument_id, manager_email),
                FOREIGN KEY (instrument_id)
                    REFERENCES instruments(id)
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
                reservation_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                purpose TEXT NOT NULL,
                purpose_other TEXT NOT NULL DEFAULT '',
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


# ============================================================
# PIN
# ============================================================

def hash_pin(pin: str, salt_hex: str | None = None) -> tuple[str, str]:
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    pin_hash = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        salt,
        200_000,
    ).hex()

    return salt_hex, pin_hash


def verify_pin(pin: str, salt_hex: str, stored_hash: str) -> bool:
    _, calculated_hash = hash_pin(pin, salt_hex)
    return hmac.compare_digest(calculated_hash, stored_hash)


# ============================================================
# Time helpers
# ============================================================

def generate_time_options() -> list[str]:
    options: list[str] = []
    current = datetime.combine(date.today(), time(0, 0))

    for _ in range(24 * 4):
        options.append(current.strftime("%H:%M"))
        current += timedelta(minutes=15)

    return options


TIME_OPTIONS = generate_time_options()


def to_minutes(value: str) -> int:
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def overlaps(
    start_a: str,
    end_a: str,
    start_b: str,
    end_b: str,
) -> bool:
    return (
        to_minutes(start_a) < to_minutes(end_b)
        and to_minutes(end_a) > to_minutes(start_b)
    )


# ============================================================
# Data access
# ============================================================

def get_instruments(active_only: bool = True) -> list[sqlite3.Row]:
    query = "SELECT * FROM instruments"
    params: tuple[Any, ...] = ()

    if active_only:
        query += " WHERE active = 1"

    query += " ORDER BY name"

    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_instrument(instrument_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM instruments WHERE id = ?",
            (instrument_id,),
        ).fetchone()


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


def get_reservations(
    instrument_id: int,
    target_date: date | None = None,
) -> list[sqlite3.Row]:
    query = """
        SELECT
            r.*,
            i.name AS instrument_name
        FROM reservations r
        JOIN instruments i ON i.id = r.instrument_id
        WHERE r.instrument_id = ?
    """
    params: list[Any] = [instrument_id]

    if target_date is not None:
        query += " AND r.reservation_date = ?"
        params.append(target_date.isoformat())

    query += """
        ORDER BY
            r.reservation_date,
            r.start_time,
            r.end_time
    """

    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_all_reservations() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                r.*,
                i.name AS instrument_name
            FROM reservations r
            JOIN instruments i ON i.id = r.instrument_id
            ORDER BY
                r.reservation_date DESC,
                r.start_time
            """
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
    params: list[Any] = [instrument_id]

    if target_date is not None:
        query += " AND reservation_date = ?"
        params.append(target_date.isoformat())

    query += " ORDER BY reservation_date, start_time"

    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def reservation_has_conflict(
    instrument_id: int,
    reservation_date: date,
    start_time: str,
    end_time: str,
) -> tuple[bool, str]:
    reservations = get_reservations(instrument_id, reservation_date)

    for reservation in reservations:
        if overlaps(
            start_time,
            end_time,
            reservation["start_time"],
            reservation["end_time"],
        ):
            return True, "指定した時間帯には既に予約があります。"

    blocked_periods = get_blocked_periods(instrument_id, reservation_date)

    for blocked in blocked_periods:
        if overlaps(
            start_time,
            end_time,
            blocked["start_time"],
            blocked["end_time"],
        ):
            reason = blocked["reason"].strip()
            message = "指定した時間帯は使用停止期間です。"
            if reason:
                message += f" 理由：{reason}"
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
    pin_salt, pin_hash = hash_pin(pin)

    custom_fields = {
        field["id"]: field
        for field in get_custom_fields(instrument_id)
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

        reservation_id = cursor.lastrowid

        for field_id, value in custom_values.items():
            field = custom_fields.get(field_id)
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


def delete_reservation(reservation_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM reservations WHERE id = ?",
            (reservation_id,),
        )


def get_reservation_details(reservation_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                r.*,
                i.name AS instrument_name
            FROM reservations r
            JOIN instruments i ON i.id = r.instrument_id
            WHERE r.id = ?
            """,
            (reservation_id,),
        ).fetchone()


def get_reservation_custom_values(
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
# Authentication and authorization
# ============================================================

def is_logged_in() -> bool:
    return bool(getattr(st.user, "is_logged_in", False))


def current_user_email() -> str:
    if not is_logged_in():
        return ""

    return str(getattr(st.user, "email", "")).strip().lower()


def current_user_name() -> str:
    if not is_logged_in():
        return ""

    return str(
        getattr(st.user, "name", "")
        or getattr(st.user, "email", "")
    ).strip()


def is_system_admin(email: str) -> bool:
    return email.lower() in SYSTEM_ADMIN_EMAILS


def get_managed_instrument_ids(email: str) -> list[int]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT instrument_id
            FROM instrument_managers
            WHERE lower(manager_email) = lower(?)
            """,
            (email,),
        ).fetchall()

    return [row["instrument_id"] for row in rows]


def can_manage_instrument(email: str, instrument_id: int) -> bool:
    return (
        is_system_admin(email)
        or instrument_id in get_managed_instrument_ids(email)
    )


# ============================================================
# Rendering helpers
# ============================================================

def reservation_purpose_label(row: sqlite3.Row) -> str:
    if row["purpose"] == "その他":
        detail = row["purpose_other"].strip()
        return f"その他（{detail}）" if detail else "その他"

    return row["purpose"]


def make_schedule_dataframe(
    reservations: list[sqlite3.Row],
    blocked_periods: list[sqlite3.Row],
) -> pd.DataFrame:
    rows: list[dict[str, str]] = []

    for reservation in reservations:
        rows.append(
            {
                "開始": reservation["start_time"],
                "終了": reservation["end_time"],
                "予約者": reservation["user_name"],
                "所属": reservation["affiliation"],
                "使用目的": reservation_purpose_label(reservation),
                "状態": "予約",
            }
        )

    for blocked in blocked_periods:
        rows.append(
            {
                "開始": blocked["start_time"],
                "終了": blocked["end_time"],
                "予約者": "",
                "所属": "",
                "使用目的": blocked["reason"],
                "状態": "使用停止",
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
        .sort_values(["開始", "終了"])
        .reset_index(drop=True)
    )


def render_custom_field(
    field: sqlite3.Row,
    key_prefix: str,
) -> Any:
    label = field["field_name"]
    if field["required"]:
        label += " *"

    key = f"{key_prefix}_{field['id']}"
    field_type = field["field_type"]
    options = json.loads(field["options_json"] or "[]")

    if field_type == "text":
        return st.text_input(label, key=key)

    if field_type == "textarea":
        return st.text_area(label, key=key)

    if field_type == "select":
        display_options = ["選択してください"] + options
        value = st.selectbox(label, display_options, key=key)
        return "" if value == "選択してください" else value

    if field_type == "multiselect":
        return st.multiselect(label, options, key=key)

    if field_type == "number":
        return st.number_input(
            label,
            min_value=0.0,
            step=1.0,
            key=key,
        )

    if field_type == "checkbox":
        return st.checkbox(label, key=key)

    return st.text_input(label, key=key)


def is_custom_value_empty(field: sqlite3.Row, value: Any) -> bool:
    field_type = field["field_type"]

    if field_type == "checkbox":
        return value is False

    if field_type == "multiselect":
        return len(value) == 0

    if value is None:
        return True

    if isinstance(value, str):
        return value.strip() == ""

    return False


# ============================================================
# User pages
# ============================================================

def page_schedule() -> None:
    st.header("予約状況")

    instruments = get_instruments(active_only=True)

    if not instruments:
        st.info("現在、予約可能な機器は登録されていません。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    col1, col2 = st.columns(2)

    with col1:
        selected_name = st.selectbox(
            "機器",
            list(instrument_map.keys()),
            key="schedule_instrument",
        )

    with col2:
        selected_date = st.date_input(
            "使用日",
            value=date.today(),
            format="YYYY/MM/DD",
            key="schedule_date",
        )

    instrument_id = instrument_map[selected_name]
    instrument = get_instrument(instrument_id)

    if instrument and instrument["notice"].strip():
        st.info(instrument["notice"])

    reservations = get_reservations(instrument_id, selected_date)
    blocked_periods = get_blocked_periods(instrument_id, selected_date)

    schedule_df = make_schedule_dataframe(
        reservations,
        blocked_periods,
    )

    if schedule_df.empty:
        st.success("この日の予約はありません。")
    else:
        st.dataframe(
            schedule_df,
            hide_index=True,
            use_container_width=True,
        )


def page_new_reservation() -> None:
    st.header("新規予約")

    instruments = get_instruments(active_only=True)

    if not instruments:
        st.info("現在、予約可能な機器は登録されていません。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    selected_name = st.selectbox(
        "機器 *",
        list(instrument_map.keys()),
        key="reservation_instrument",
    )

    instrument_id = instrument_map[selected_name]
    instrument = get_instrument(instrument_id)

    if instrument is not None:
        if instrument["description"].strip():
            st.caption(instrument["description"])

        if instrument["notice"].strip():
            st.info(instrument["notice"])

    custom_fields = get_custom_fields(instrument_id)

    with st.form(
        f"reservation_form_{instrument_id}",
        clear_on_submit=False,
    ):
        user_name = st.text_input("氏名 *")
        affiliation = st.text_input("所属 *")

        reservation_date = st.date_input(
            "使用日 *",
            value=date.today(),
            min_value=date.today(),
            format="YYYY/MM/DD",
        )

        col1, col2 = st.columns(2)

        with col1:
            start_time = st.selectbox(
                "開始時刻 *",
                TIME_OPTIONS,
                index=36,  # 09:00
            )

        with col2:
            end_time = st.selectbox(
                "終了時刻 *",
                TIME_OPTIONS,
                index=40,  # 10:00
            )

        purpose = st.selectbox(
            "使用目的 *",
            ["測定", "解析のみ", "その他"],
        )

        purpose_other = ""
        if purpose == "その他":
            purpose_other = st.text_input(
                "「その他」の内容 *"
            )

        custom_values: dict[int, Any] = {}

        if custom_fields:
            st.subheader("機器固有の入力項目")

            for field in custom_fields:
                custom_values[field["id"]] = render_custom_field(
                    field,
                    key_prefix=f"reservation_{instrument_id}",
                )

        pin = st.text_input(
            "4桁の暗証番号 *",
            type="password",
            max_chars=4,
            help="予約を取り消す際に必要です。",
        )

        submitted = st.form_submit_button(
            "予約する",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return

    errors: list[str] = []

    if not user_name.strip():
        errors.append("氏名を入力してください。")

    if not affiliation.strip():
        errors.append("所属を入力してください。")

    if to_minutes(start_time) >= to_minutes(end_time):
        errors.append(
            "終了時刻は開始時刻より後の時刻を選択してください。"
        )

    if purpose == "その他" and not purpose_other.strip():
        errors.append(
            "「その他」を選択した場合は内容を入力してください。"
        )

    if not (pin.isdigit() and len(pin) == 4):
        errors.append("暗証番号は4桁の数字で入力してください。")

    for field in custom_fields:
        value = custom_values[field["id"]]

        if field["required"] and is_custom_value_empty(field, value):
            errors.append(
                f"「{field['field_name']}」を入力してください。"
            )

    if errors:
        for error in errors:
            st.error(error)
        return

    has_conflict, conflict_message = reservation_has_conflict(
        instrument_id,
        reservation_date,
        start_time,
        end_time,
    )

    if has_conflict:
        st.error(conflict_message)
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
        f"{selected_name}を"
        f"{reservation_date.strftime('%Y年%m月%d日')} "
        f"{start_time}〜{end_time}で予約しました。"
    )


def page_cancel_reservation() -> None:
    st.header("予約取消")

    instruments = get_instruments(active_only=True)

    if not instruments:
        st.info("予約可能な機器は登録されていません。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    col1, col2 = st.columns(2)

    with col1:
        selected_name = st.selectbox(
            "機器",
            list(instrument_map.keys()),
            key="cancel_instrument",
        )

    with col2:
        selected_date = st.date_input(
            "使用日",
            value=date.today(),
            format="YYYY/MM/DD",
            key="cancel_date",
        )

    instrument_id = instrument_map[selected_name]
    reservations = get_reservations(
        instrument_id,
        selected_date,
    )

    if not reservations:
        st.info("この日の予約はありません。")
        return

    reservation_map: dict[str, int] = {}

    for reservation in reservations:
        label = (
            f"{reservation['start_time']}〜"
            f"{reservation['end_time']} ｜ "
            f"{reservation['user_name']} ｜ "
            f"{reservation['affiliation']}"
        )
        reservation_map[label] = reservation["id"]

    selected_label = st.selectbox(
        "取り消す予約",
        list(reservation_map.keys()),
    )

    reservation_id = reservation_map[selected_label]
    reservation = get_reservation_details(reservation_id)

    if reservation is None:
        st.error("予約情報を取得できませんでした。")
        return

    st.write(
        f"**使用目的：** "
        f"{reservation_purpose_label(reservation)}"
    )

    pin = st.text_input(
        "4桁の暗証番号",
        type="password",
        max_chars=4,
        key=f"cancel_pin_{reservation_id}",
    )

    if st.button(
        "予約を取り消す",
        type="primary",
        use_container_width=True,
    ):
        if not verify_pin(
            pin,
            reservation["pin_salt"],
            reservation["pin_hash"],
        ):
            st.error("暗証番号が正しくありません。")
            return

        delete_reservation(reservation_id)
        st.success("予約を取り消しました。")
        st.rerun()


# ============================================================
# Management pages
# ============================================================

def render_login_area() -> None:
    st.header("管理者ログイン")

    if not is_logged_in():
        st.write(
            "システム管理者または機器管理者は、"
            "Microsoftアカウントでログインしてください。"
        )

        if st.button(
            "Microsoftアカウントでログイン",
            type="primary",
        ):
            st.login("microsoft")

        return

    email = current_user_email()
    name = current_user_name()

    st.success(f"ログイン中：{name}（{email}）")

    if st.button("ログアウト"):
        st.logout()


def management_instrument_ids(email: str) -> list[int]:
    if is_system_admin(email):
        return [
            instrument["id"]
            for instrument in get_instruments(active_only=False)
        ]

    return get_managed_instrument_ids(email)


def page_management() -> None:
    render_login_area()

    if not is_logged_in():
        return

    email = current_user_email()
    managed_ids = management_instrument_ids(email)

    if not managed_ids and not is_system_admin(email):
        st.warning("管理権限が登録されていません。")
        return

    tabs = []

    if is_system_admin(email):
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
            manager_instrument_settings(email)

        with tabs[3]:
            manager_reservation_management(email)

    else:
        tabs = st.tabs(
            [
                "担当機器設定",
                "予約管理",
            ]
        )

        with tabs[0]:
            manager_instrument_settings(email)

        with tabs[1]:
            manager_reservation_management(email)


def admin_instrument_management() -> None:
    st.subheader("機器管理")

    with st.form("add_instrument_form"):
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

                st.success("機器を追加しました。")
                st.rerun()

            except sqlite3.IntegrityError:
                st.error("同じ名前の機器が既に登録されています。")

    instruments = get_instruments(active_only=False)

    if not instruments:
        st.info("機器はまだ登録されていません。")
        return

    st.divider()

    for instrument in instruments:
        with st.expander(instrument["name"]):
            new_name = st.text_input(
                "機器名",
                value=instrument["name"],
                key=f"instrument_name_{instrument['id']}",
            )

            new_description = st.text_area(
                "説明",
                value=instrument["description"],
                key=f"instrument_description_{instrument['id']}",
            )

            new_notice = st.text_area(
                "利用者への注意事項",
                value=instrument["notice"],
                key=f"instrument_notice_{instrument['id']}",
            )

            active = st.checkbox(
                "予約可能",
                value=bool(instrument["active"]),
                key=f"instrument_active_{instrument['id']}",
            )

            if st.button(
                "保存",
                key=f"save_instrument_{instrument['id']}",
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

                    st.success("保存しました。")
                    st.rerun()

                except sqlite3.IntegrityError:
                    st.error(
                        "同じ名前の機器が既に登録されています。"
                    )


def admin_manager_management() -> None:
    st.subheader("機器管理者")

    instruments = get_instruments(active_only=False)

    if not instruments:
        st.info("先に機器を登録してください。")
        return

    instrument_map = {
        instrument["name"]: instrument["id"]
        for instrument in instruments
    }

    with st.form("add_manager_form"):
        selected_name = st.selectbox(
            "機器",
            list(instrument_map.keys()),
        )

        manager_email = st.text_input(
            "機器管理者のMicrosoftアカウント"
        )

        submitted = st.form_submit_button("管理者を追加")

    if submitted:
        if not manager_email.strip():
            st.error("Microsoftアカウントを入力してください。")
        else:
            try:
                with get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO instrument_managers (
                            instrument_id,
                            manager_email
                        )
                        VALUES (?, ?)
                        """,
                        (
                            instrument_map[selected_name],
                            manager_email.strip().lower(),
                        ),
                    )

                st.success("機器管理者を追加しました。")
                st.rerun()

            except sqlite3.IntegrityError:
                st.error(
                    "このアカウントは既に管理者として登録されています。"
                )

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                im.id,
                i.name AS instrument_name,
                im.manager_email
            FROM instrument_managers im
            JOIN instruments i ON i.id = im.instrument_id
            ORDER BY i.name, im.manager_email
            """
        ).fetchall()

    if not rows:
        st.info("機器管理者は登録されていません。")
        return

    st.divider()

    for row in rows:
        col1, col2 = st.columns([5, 1])

        with col1:
            st.write(
                f"**{row['instrument_name']}** ｜ "
                f"{row['manager_email']}"
            )

        with col2:
            if st.button(
                "解除",
                key=f"delete_manager_{row['id']}",
            ):
                with get_connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM instrument_managers
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )

                st.rerun()


def manager_instrument_settings(email: str) -> None:
    st.subheader("担当機器設定")

    instrument_ids = management_instrument_ids(email)

    instruments = [
        get_instrument(instrument_id)
        for instrument_id in instrument_ids
    ]

    instruments = [
        instrument
        for instrument in instruments
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
        key="manager_selected_instrument",
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
        key=f"save_manager_settings_{instrument_id}",
    ):
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE instruments
                SET description = ?, notice = ?
                WHERE id = ?
                """,
                (
                    description.strip(),
                    notice.strip(),
                    instrument_id,
                ),
            )

        st.success("保存しました。")
        st.rerun()

    st.divider()
    custom_field_management(instrument_id)

    st.divider()
    blocked_period_management(instrument_id)


def custom_field_management(instrument_id: int) -> None:
    st.markdown("### 予約入力項目")

    field_type_labels = {
        "一行テキスト": "text",
        "複数行テキスト": "textarea",
        "単一選択": "select",
        "複数選択": "multiselect",
        "数値": "number",
        "チェックボックス": "checkbox",
    }

    with st.form(f"add_custom_field_{instrument_id}"):
        field_name = st.text_input("項目名 *")

        field_type_label = st.selectbox(
            "入力形式",
            list(field_type_labels.keys()),
        )

        required = st.checkbox("必須項目にする")

        options_text = st.text_area(
            "選択肢",
            help=(
                "単一選択または複数選択の場合、"
                "1行に1つずつ入力してください。"
            ),
        )

        submitted = st.form_submit_button("入力項目を追加")

    if submitted:
        field_type = field_type_labels[field_type_label]

        options = [
            line.strip()
            for line in options_text.splitlines()
            if line.strip()
        ]

        errors: list[str] = []

        if not field_name.strip():
            errors.append("項目名を入力してください。")

        if field_type in {"select", "multiselect"} and not options:
            errors.append("選択肢を入力してください。")

        if errors:
            for error in errors:
                st.error(error)
        else:
            with get_connection() as conn:
                max_order_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(display_order), 0) AS max_order
                    FROM custom_fields
                    WHERE instrument_id = ?
                    """,
                    (instrument_id,),
                ).fetchone()

                next_order = max_order_row["max_order"] + 1

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
                        next_order,
                    ),
                )

            st.success("入力項目を追加しました。")
            st.rerun()

    fields = get_custom_fields(instrument_id)

    if not fields:
        st.caption("追加の入力項目はありません。")
        return

    type_name_map = {
        "text": "一行テキスト",
        "textarea": "複数行テキスト",
        "select": "単一選択",
        "multiselect": "複数選択",
        "number": "数値",
        "checkbox": "チェックボックス",
    }

    for field in fields:
        col1, col2, col3, col4 = st.columns([4, 2, 1, 1])

        with col1:
            st.write(f"**{field['field_name']}**")

            options = json.loads(field["options_json"] or "[]")
            if options:
                st.caption(" / ".join(options))

        with col2:
            st.write(type_name_map.get(field["field_type"], ""))

        with col3:
            st.write("必須" if field["required"] else "任意")

        with col4:
            if st.button(
                "削除",
                key=f"delete_field_{field['id']}",
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

    with st.form(f"blocked_period_form_{instrument_id}"):
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
                key=f"blocked_start_{instrument_id}",
            )

        with col2:
            end_time = st.selectbox(
                "終了時刻",
                TIME_OPTIONS,
                index=68,
                key=f"blocked_end_{instrument_id}",
            )

        reason = st.text_input("理由")

        submitted = st.form_submit_button("使用停止期間を追加")

    if submitted:
        if to_minutes(start_time) >= to_minutes(end_time):
            st.error(
                "終了時刻は開始時刻より後の時刻を選択してください。"
            )
        else:
            has_conflict, conflict_message = reservation_has_conflict(
                instrument_id,
                blocked_date,
                start_time,
                end_time,
            )

            if has_conflict:
                st.error(
                    "この時間帯には予約または使用停止期間があります。"
                    f" {conflict_message}"
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

                st.success("使用停止期間を追加しました。")
                st.rerun()

    blocked_periods = get_blocked_periods(instrument_id)

    for blocked in blocked_periods:
        col1, col2 = st.columns([6, 1])

        with col1:
            text = (
                f"{blocked['reservation_date']} ｜ "
                f"{blocked['start_time']}〜{blocked['end_time']}"
            )

            if blocked["reason"]:
                text += f" ｜ {blocked['reason']}"

            st.write(text)

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


def manager_reservation_management(email: str) -> None:
    st.subheader("予約管理")

    instrument_ids = management_instrument_ids(email)

    instruments = [
        get_instrument(instrument_id)
        for instrument_id in instrument_ids
    ]

    instruments = [
        instrument
        for instrument in instruments
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

    instrument_id = instrument_map[selected_name]
    reservations = get_reservations(instrument_id)

    if not reservations:
        st.info("予約はありません。")
        return

    for reservation in reservations:
        title = (
            f"{reservation['reservation_date']} "
            f"{reservation['start_time']}〜"
            f"{reservation['end_time']} ｜ "
            f"{reservation['user_name']}"
        )

        with st.expander(title):
            st.write(f"**所属：** {reservation['affiliation']}")
            st.write(
                f"**使用目的：** "
                f"{reservation_purpose_label(reservation)}"
            )

            custom_values = get_reservation_custom_values(
                reservation["id"]
            )

            for custom_value in custom_values:
                value = json.loads(custom_value["value_json"])
                st.write(
                    f"**{custom_value['field_name_snapshot']}：** "
                    f"{value}"
                )

            if st.button(
                "管理者権限で予約を削除",
                key=f"manager_delete_reservation_{reservation['id']}",
            ):
                delete_reservation(reservation["id"])
                st.success("予約を削除しました。")
                st.rerun()


# ============================================================
# Main
# ============================================================

def main() -> None:
    init_db()

    st.title(APP_TITLE)
    st.caption("Equipment Booking System")

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
