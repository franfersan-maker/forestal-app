from __future__ import annotations

import os
from typing import Any

from supabase import create_client


class SupabaseApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


class SupabaseClient:
    """Supabase client configured only through environment variables."""

    def __init__(self) -> None:
        try:
            self.supabase_url = os.environ["SUPABASE_URL"].strip().rstrip("/")
            self.supabase_key = os.environ["SUPABASE_KEY"].strip()
        except KeyError as error:
            raise SupabaseApiError(
                "Falta la variable de entorno requerida para Supabase.", 500
            ) from error
        if not self.supabase_url or not self.supabase_key:
            raise SupabaseApiError(
                "Faltan SUPABASE_URL o SUPABASE_KEY en las variables de entorno.", 500
            )
        self.client = create_client(self.supabase_url, self.supabase_key)

    @staticmethod
    def _raise_api_error(error: Exception) -> SupabaseApiError:
        message = getattr(error, "message", None) or str(error)
        raw_status = getattr(error, "code", 0)
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            status = 0
        return SupabaseApiError(str(message), status)

    def select(self, table: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        params = params or {}
        query = self.client.table(table).select(params.get("select", "*"))
        try:
            for column, filter_value in params.items():
                if column in {"select", "order", "limit", "offset"}:
                    continue
                if filter_value.startswith("eq."):
                    query = query.eq(column, filter_value[3:])
                elif filter_value.startswith("ilike."):
                    query = query.ilike(column, filter_value[6:])
            for order_item in params.get("order", "").split(","):
                if order_item:
                    parts = order_item.split(".")
                    query = query.order(parts[0], desc=len(parts) > 1 and parts[1] == "desc")
            limit = int(params["limit"]) if params.get("limit") else None
            offset = int(params.get("offset", "0"))
            if limit is not None:
                query = query.range(offset, offset + limit - 1)
            result = query.execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as error:
            raise self._raise_api_error(error) from error

    def insert(
        self,
        table: str,
        rows: dict[str, Any] | list[dict[str, Any]],
        return_representation: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            query = self.client.table(table).insert(rows)
            result = query.execute()
            if not return_representation:
                return []
            return result.data if isinstance(result.data, list) else []
        except Exception as error:
            raise self._raise_api_error(error) from error