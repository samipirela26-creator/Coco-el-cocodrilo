"""Cache de tasas de cambio (global, no depende del perfil -- son precios de
mercado, no datos personales). `FxCacheMixin` se combina con los demás
mixins dentro de `DBClient` (ver src/storage/db.py).
"""
from datetime import datetime


class FxCacheMixin:
    def get_cached_fx_rate(self, page: str) -> tuple:
        """Retorna (rate, fetched_at) o (None, None) si no hay cache."""
        row = self._conn.execute(
            "SELECT rate, fetched_at FROM fx_rates WHERE page = ?", (page,)
        ).fetchone()
        if row:
            return float(row[0]), row[1]
        return None, None

    def set_cached_fx_rate(self, page: str, rate: float) -> None:
        self._conn.execute(
            "INSERT INTO fx_rates (page, rate, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(page) DO UPDATE SET rate = excluded.rate, fetched_at = excluded.fetched_at",
            (page, rate, datetime.now().isoformat())
        )
        self._conn.commit()
