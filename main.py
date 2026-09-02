from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

print("App iniciada", flush=True)

import streamlit as st

try:
    import pandas as pd
    PANDAS_IMPORT_ERROR: Exception | None = None
except Exception as error:
    pd = None  # type: ignore[assignment]
    PANDAS_IMPORT_ERROR = error

try:
    from supabase_client import SupabaseApiError, SupabaseClient
    SUPABASE_IMPORT_ERROR: Exception | None = None
except Exception as error:
    SupabaseApiError = RuntimeError  # type: ignore[misc,assignment]
    SupabaseClient = None  # type: ignore[assignment]
    SUPABASE_IMPORT_ERROR = error


APP_NAME = "Forestal App"
ROLES = {
    "jefe": {"label": "Jefe", "can_upload": True},
    "capataz": {"label": "Capataz", "can_upload": True},
    "admin": {"label": "Administrador", "can_upload": True},
}


def password_matches(password: str, encoded_hash: str) -> bool:
    """Validate the PBKDF2 format used by the setup SQL."""
    try:
        algorithm, iterations, salt, expected = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.urlsafe_b64decode(salt + "==="),
            int(iterations),
        )
        actual = base64.urlsafe_b64encode(derived).decode("ascii").rstrip("=")
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def authenticate(client: SupabaseClient, username: str, password: str) -> dict[str, Any] | None:
    username = username.strip().lower()
    if not username or not password:
        return None
    rows = client.select(
        "app_users",
        {
            "select": "username,display_name,role,password_hash",
            "username": f"eq.{username}",
            "limit": "1",
        },
    )
    if not rows:
        return None
    user = rows[0]
    role = str(user.get("role", "")).lower()
    if role not in ROLES or not password_matches(password, str(user.get("password_hash", ""))):
        return None
    return {
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": role,
    }


def normalize_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def parse_workbook(uploaded_file: Any) -> tuple[list[dict[str, Any]], int]:
    raw = uploaded_file.getvalue()
    workbook = pd.ExcelFile(io.BytesIO(raw))
    rows: list[dict[str, Any]] = []
    for sheet_name in workbook.sheet_names:
        frame = pd.read_excel(workbook, sheet_name=sheet_name, dtype=object)
        frame = frame.dropna(how="all")
        frame.columns = [str(column).strip() or "Columna sin nombre" for column in frame.columns]
        for row_number, values in enumerate(frame.to_dict(orient="records"), start=2):
            rows.append(
                {
                    "sheet_name": str(sheet_name),
                    "row_number": row_number,
                    "data": {str(key): normalize_value(value) for key, value in values.items()},
                }
            )
    return rows, len(workbook.sheet_names)


def safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "-", name).strip() or "Archivo"
    cleaned = cleaned[:31]
    candidate = cleaned
    suffix = 2
    while candidate in used:
        suffix_text = f" ({suffix})"
        candidate = f"{cleaned[: 31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def fetch_all_rows(client: SupabaseClient) -> list[dict[str, Any]]:
    page_size = 1000
    offset = 0
    result: list[dict[str, Any]] = []
    while True:
        page = client.select(
            "file_rows",
            {
                "select": "file_id,row_number,sheet_name,data",
                "order": "file_id.asc,row_number.asc",
                "limit": str(page_size),
                "offset": str(offset),
            },
        )
        result.extend(page)
        if len(page) < page_size:
            return result
        offset += page_size


def create_export(files: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bytes:
    file_names = {str(item["id"]): str(item["file_name"]) for item in files}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = f"{file_names.get(str(row.get('file_id')), 'Archivo')} — {row.get('sheet_name', 'Hoja')}"
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        grouped.setdefault(key, []).append(
            {
                "archivo_origen": file_names.get(str(row.get("file_id")), "Archivo"),
                "hoja": row.get("sheet_name", ""),
                "fila_excel": row.get("row_number", ""),
                **data,
            }
        )

    output = io.BytesIO()
    used_sheets: set[str] = set()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary = pd.DataFrame(
            [
                {
                    "archivo": item.get("file_name", ""),
                    "hojas": item.get("sheet_count", 0),
                    "filas": item.get("row_count", 0),
                    "subido_por": item.get("uploaded_by", ""),
                    "fecha": item.get("created_at", ""),
                }
                for item in files
            ]
        )
        summary.to_excel(writer, sheet_name="Resumen", index=False)
        for group_name, group_rows in grouped.items():
            frame = pd.DataFrame(group_rows)
            frame.to_excel(writer, sheet_name=safe_sheet_name(group_name, used_sheets), index=False)
    return output.getvalue()


def show_setup_error(error: SupabaseApiError) -> None:
    st.error("No se pudo completar la conexión con Supabase.")
    if error.status == 404 or "schema cache" in error.message.lower():
        st.info(
            "La conexión funciona, pero todavía faltan las tablas de Forestal App. "
            "Ejecuta el archivo `supabase_schema.sql` en el SQL Editor de tu proyecto Supabase."
        )
        st.download_button(
            "Descargar esquema de Supabase",
            data=Path("supabase_schema.sql").read_bytes(),
            file_name="supabase_schema.sql",
            mime="application/sql",
        )
    else:
        st.code(sanitize_error(error))


def sanitize_error(error: Exception) -> str:
    """Keep configuration secrets out of the visible diagnostic message."""
    return sanitize_text(str(error) or error.__class__.__name__)


def sanitize_text(text: str) -> str:
    """Remove configured secret values from any visible diagnostic text."""
    for secret_name in ("SUPABASE_KEY", "SUPABASE_URL"):
        secret = os.environ.get(secret_name)
        if secret:
            text = text.replace(secret, f"<{secret_name}>")
    return text


def show_runtime_error(error: Exception) -> None:
    st.error("La aplicación encontró un error y no se ha quedado bloqueada.")
    st.write("Revisa el detalle siguiente para saber qué corregir:")
    st.code(f"{error.__class__.__name__}: {sanitize_error(error)}")
    with st.expander("Ver diagnóstico técnico"):
        full_traceback = "".join(traceback.TracebackException.from_exception(error).format())
        st.code(sanitize_text(full_traceback))
    st.info(
        "Si el error menciona tablas inexistentes, ejecuta `supabase_schema.sql` "
        "en el SQL Editor de Supabase."
    )


def render_login(client: SupabaseClient) -> None:
    st.title(APP_NAME)
    st.subheader("Control de cargas forestales")
    st.write("Ingresa con tu usuario para consultar y consolidar los archivos de operación.")
    with st.form("login_form"):
        username = st.text_input("Usuario", placeholder="jefe, capataz o admin")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar", type="primary", use_container_width=True)
    if submitted:
        try:
            user = authenticate(client, username, password)
            if user is None:
                st.error("Usuario o contraseña incorrectos.")
            else:
                st.session_state["user"] = user
                st.rerun()
        except SupabaseApiError as error:
            show_setup_error(error)


def render_dashboard(client: SupabaseClient, user: dict[str, Any]) -> None:
    role = str(user["role"])
    role_config = ROLES[role]
    with st.sidebar:
        st.title(APP_NAME)
        st.caption("Control de cargas forestales")
        st.divider()
        st.write(f"**{user['display_name']}**")
        st.caption(f"Rol: {role_config['label']}")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.pop("user", None)
            st.rerun()

    st.title("Centro de archivos")
    st.write("Carga tus tres planillas de operación y descarga una única versión consolidada.")

    files = client.select(
        "file_uploads",
        {
            "select": "id,file_name,file_size,sheet_count,row_count,uploaded_by,created_at",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    total_rows = sum(int(item.get("row_count") or 0) for item in files)
    first, second, third = st.columns(3)
    first.metric("Archivos guardados", len(files))
    second.metric("Filas procesadas", total_rows)
    third.metric("Última carga", files[0].get("created_at", "")[:10] if files else "—")

    if role_config["can_upload"]:
        st.subheader("Nueva carga")
        selected_files = st.file_uploader(
            "Selecciona exactamente 3 archivos Excel",
            type=["xlsx", "xls"],
            accept_multiple_files=True,
            help="Se conservan todas las hojas y se añade el nombre del archivo y de la hoja al consolidado.",
        )
        selected_count = len(selected_files or [])
        if selected_count:
            st.caption(f"{selected_count} de 3 archivos seleccionados")
        if selected_count > 3:
            st.warning("Selecciona solo 3 archivos.")
        elif selected_count and selected_count < 3:
            st.info("Faltan archivos para completar la carga.")
        if st.button(
            "Procesar y guardar los 3 archivos",
            type="primary",
            disabled=selected_count != 3,
            use_container_width=True,
        ):
            progress = st.progress(0, text="Preparando archivos…")
            try:
                for index, uploaded_file in enumerate(selected_files or []):
                    parsed_rows, sheet_count = parse_workbook(uploaded_file)
                    metadata = client.insert(
                        "file_uploads",
                        {
                            "file_name": uploaded_file.name,
                            "file_size": uploaded_file.size,
                            "sheet_count": sheet_count,
                            "row_count": len(parsed_rows),
                            "uploaded_by": user["username"],
                        },
                        return_representation=True,
                    )
                    file_id = metadata[0]["id"] if metadata else None
                    if not file_id:
                        raise SupabaseApiError("Supabase no devolvió el identificador de la carga.", 500)
                    for start in range(0, len(parsed_rows), 500):
                        batch = [
                            {
                                "file_id": file_id,
                                "row_number": item["row_number"],
                                "sheet_name": item["sheet_name"],
                                "data": item["data"],
                            }
                            for item in parsed_rows[start : start + 500]
                        ]
                        if batch:
                            client.insert("file_rows", batch)
                    progress.progress((index + 1) / 3, text=f"Guardado: {uploaded_file.name}")
                st.success("Los 3 archivos se procesaron y guardaron correctamente.")
                st.rerun()
            except (SupabaseApiError, ValueError, OSError) as error:
                progress.empty()
                st.error("No se pudo procesar la carga.")
                show_runtime_error(error)

    st.subheader("Archivos disponibles")
    if files:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Archivo": item.get("file_name", ""),
                        "Hojas": item.get("sheet_count", 0),
                        "Filas": item.get("row_count", 0),
                        "Subido por": item.get("uploaded_by", ""),
                        "Fecha": str(item.get("created_at", ""))[:19].replace("T", " "),
                    }
                    for item in files
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        if st.button("Preparar exportación completa", use_container_width=True):
            with st.spinner("Construyendo el Excel consolidado…"):
                all_rows = fetch_all_rows(client)
                export_data = create_export(files, all_rows)
            st.download_button(
                "Descargar forestal_consolidado.xlsx",
                data=export_data,
                file_name=f"forestal_consolidado_{datetime.now(timezone.utc):%Y%m%d_%H%M}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
    else:
        st.info("Todavía no hay archivos guardados.")


def main() -> None:
    try:
        st.set_page_config(page_title=APP_NAME, page_icon="F", layout="wide")
        if PANDAS_IMPORT_ERROR is not None:
            show_runtime_error(PANDAS_IMPORT_ERROR)
            return
        if SUPABASE_IMPORT_ERROR is not None:
            show_runtime_error(SUPABASE_IMPORT_ERROR)
            return
        client = SupabaseClient()
        user = st.session_state.get("user")
        if user:
            render_dashboard(client, user)
        else:
            render_login(client)
    except SupabaseApiError as error:
        show_setup_error(error)
    except Exception as error:
        show_runtime_error(error)


if __name__ == "__main__":
    main()
