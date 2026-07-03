"""Metas de ahorro (ej. "quiero ahorrar 500 dólares para un viaje"). `SavingsMixin`
se combina con los demás mixins dentro de `DBClient` (ver src/storage/db.py).

A propósito NO toca ninguna billetera: aportar a una meta es un gesto de
apartar/reservar dinero mentalmente, no un movimiento nuevo (el dinero
normalmente ya está en alguna billetera del usuario) -- mismo patrón que
tithes.py y debts.py. Es solo un contador de progreso hacia un objetivo.
"""
import logging
from datetime import datetime
from src.storage.constants import MONEDAS_VALIDAS

logger = logging.getLogger('gastos-bot')


class SavingsMixin:
    def create_goal(self, perfil: str, nombre: str, moneda: str, monto_objetivo: float) -> int:
        """Crea una meta de ahorro nueva DEL PERFIL dado, con progreso en 0.
        Retorna el id de la meta creada."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        nombre = (nombre or '').strip() or 'Meta sin nombre'
        ahora = datetime.now().isoformat()
        cur = self._conn.execute(
            """INSERT INTO savings_goals
               (perfil, nombre, moneda, monto_objetivo, monto_actual, cumplida, created_at)
               VALUES (?, ?, ?, ?, 0, 0, ?)""",
            (perfil, nombre, moneda, monto_objetivo, ahora)
        )
        self._conn.commit()
        logger.info(f"Meta de ahorro creada [{perfil}]: {nombre} ({moneda} {monto_objetivo})")
        return cur.lastrowid

    def _find_goal(self, perfil: str, nombre: str):
        """Busca una meta ACTIVA (no cumplida) por nombre, insensible a
        mayúsculas/minúsculas. Si hay varias con el mismo nombre, usa la más
        reciente. None si no encuentra ninguna."""
        rows = self._conn.execute(
            "SELECT id, nombre, moneda, monto_objetivo, monto_actual FROM savings_goals "
            "WHERE perfil = ? AND cumplida = 0 AND lower(nombre) = lower(?) "
            "ORDER BY created_at DESC LIMIT 1",
            (perfil, nombre)
        ).fetchone()
        return rows

    def contribute_goal(self, perfil: str, nombre: str, monto: float) -> dict:
        """Aporta `monto` a la meta ACTIVA con ese nombre DEL PERFIL dado.
        Si no existe ninguna meta activa con ese nombre, retorna
        {"encontrada": False}. Si el aporte alcanza o supera el objetivo,
        marca la meta como cumplida (el sobrante NO se descarta, se refleja
        en monto_actual tal cual)."""
        goal = self._find_goal(perfil, nombre)
        if not goal:
            return {"encontrada": False}
        goal_id, nombre_real, moneda, objetivo, actual = goal
        nuevo_actual = actual + monto
        cumplida = 1 if nuevo_actual >= objetivo else 0
        self._conn.execute(
            "UPDATE savings_goals SET monto_actual = ?, cumplida = ? WHERE id = ?",
            (nuevo_actual, cumplida, goal_id)
        )
        self._conn.commit()
        logger.info(f"Aporte a meta [{perfil}] '{nombre_real}': +{monto} (ahora {nuevo_actual}/{objetivo})")
        return {
            "encontrada": True, "nombre": nombre_real, "moneda": moneda,
            "monto_objetivo": objetivo, "monto_actual": nuevo_actual,
            "cumplida": bool(cumplida),
        }

    def list_goals(self, perfil: str, solo_activas: bool = True) -> list:
        """Lista metas de ahorro DEL PERFIL dado, opcionalmente solo las que
        aún no se han cumplido."""
        query = (
            "SELECT nombre, moneda, monto_objetivo, monto_actual, cumplida, created_at "
            "FROM savings_goals WHERE perfil = ?"
        )
        params = [perfil]
        if solo_activas:
            query += " AND cumplida = 0"
        query += " ORDER BY created_at"
        rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "nombre": n, "moneda": m, "monto_objetivo": mo, "monto_actual": ma,
                "cumplida": bool(c), "created_at": ca,
                "porcentaje": round(min(ma / mo, 1.0) * 100, 1) if mo > 0 else 0.0,
            }
            for n, m, mo, ma, c, ca in rows
        ]
