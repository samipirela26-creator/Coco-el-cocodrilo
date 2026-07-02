"""Presupuestos mensuales por categoría (en Bs), aislados por perfil.
`BudgetsMixin` se combina con los demás mixins dentro de `DBClient`
(ver src/storage/db.py).
"""
import sqlite3
from datetime import datetime
from src.utils.exceptions import StorageError


class BudgetsMixin:
    def set_budget(self, perfil: str, categoria: str, limite_mensual: float) -> None:
        """Fija (o reemplaza) el límite mensual en Bs para una categoría de
        este perfil. Un límite <= 0 elimina el presupuesto de esa categoría."""
        try:
            if limite_mensual <= 0:
                self._conn.execute(
                    "DELETE FROM budgets WHERE perfil = ? AND categoria = ?", (perfil, categoria)
                )
            else:
                self._conn.execute(
                    """INSERT INTO budgets (perfil, categoria, limite_mensual, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(perfil, categoria) DO UPDATE SET
                           limite_mensual = excluded.limite_mensual, updated_at = excluded.updated_at""",
                    (perfil, categoria, limite_mensual, datetime.now().isoformat())
                )
            self._conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Error al fijar el presupuesto: {e}")

    def get_all_budgets(self, perfil: str) -> list:
        """Retorna los presupuestos de este perfil: [{"categoria": str, "limite_mensual": float}, ...]."""
        rows = self._conn.execute(
            "SELECT categoria, limite_mensual FROM budgets WHERE perfil = ? ORDER BY categoria ASC",
            (perfil,)
        ).fetchall()
        return [{"categoria": c, "limite_mensual": lim} for c, lim in rows]

    def get_budget_status(self, perfil: str, categoria: str, fecha: str) -> dict:
        """Si esta categoría tiene un presupuesto mensual fijado, retorna
        cuánto se ha gastado en Bs en ESE MES (según `fecha`, formato
        YYYY-MM-DD) contra el límite. Si no hay presupuesto para la
        categoría, retorna None -- para no calcular de más en el camino
        feliz (sin presupuestos configurados)."""
        row = self._conn.execute(
            "SELECT limite_mensual FROM budgets WHERE perfil = ? AND categoria = ?",
            (perfil, categoria)
        ).fetchone()
        if row is None:
            return None
        limite = row[0]
        anio_mes = fecha[:7]  # "YYYY-MM"
        gastado_row = self._conn.execute(
            """SELECT COALESCE(SUM(monto), 0) FROM transactions
               WHERE perfil = ? AND categoria = ? AND tipo = 'gasto'
                 AND moneda = 'Bs' AND substr(fecha, 1, 7) = ?""",
            (perfil, categoria, anio_mes)
        ).fetchone()
        gastado = gastado_row[0] if gastado_row else 0.0
        return {
            "limite_mensual": limite,
            "gastado": gastado,
            "porcentaje": round((gastado / limite * 100), 1) if limite > 0 else 0.0,
        }
