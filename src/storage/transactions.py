"""Transacciones (gasto/ingreso), categorías dinámicas, resumen mensual,
exportación y racha de días registrando. `TransactionsMixin` se combina con
los demás mixins dentro de `DBClient` (ver src/storage/db.py).
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from src.utils.exceptions import StorageError
from src.storage.constants import MONEDAS_VALIDAS, resolve_cuenta

logger = logging.getLogger('gastos-bot')


class TransactionsMixin:
    # ------------------------------------------------------------------ #
    # Transacciones
    # ------------------------------------------------------------------ #

    def append_expense(self, perfil: str, tipo: str, fecha: str, descripcion: str,
                        categoria: str, monto: float, user_id: int = None,
                        moneda: str = 'Bs', cuenta: str = None) -> str:
        """Inserta una transacción (gasto o ingreso) y ajusta la billetera
        correspondiente DEL PERFIL dado. Si la categoría no existe aún para
        ese perfil, la crea como dinámica. Retorna la cuenta resuelta (útil
        para el mensaje de confirmación, ya que en USD puede no ser la que
        el usuario escribió)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        self._ensure_wallets_for_profile(perfil)
        cuenta = self.resolve_cuenta_perfil(perfil, moneda, cuenta)
        try:
            self._conn.execute(
                """INSERT INTO transactions
                   (tipo, monto, categoria, fecha, descripcion, user_id, created_at, moneda, cuenta, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tipo, monto, categoria, fecha, descripcion, user_id,
                 datetime.now().isoformat(), moneda, cuenta, perfil)
            )
            self._ensure_category(categoria, perfil)
            delta = monto if tipo == 'ingreso' else -monto
            cursor = self._conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? "
                "WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (delta, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            if cursor.rowcount != 1:
                # Nunca debe pasar (la billetera se garantiza arriba con
                # _ensure_wallets_for_profile), pero si algún día no matchea
                # ninguna fila, el UPDATE no falla ni avisa por sí solo --
                # la transacción quedaría insertada con el saldo sin tocar,
                # perdiendo dinero en silencio (bug real detectado en
                # producción 2026-07-08: un ingreso de 6000 COP se guardó en
                # `transactions` pero el saldo de la billetera nunca subió).
                # Mejor abortar todo y que el usuario vea un error claro.
                raise StorageError(
                    f"No se pudo actualizar la billetera {moneda}/{cuenta} "
                    f"del perfil {perfil} (rowcount={cursor.rowcount})"
                )
            self._conn.commit()
            if tipo == 'ingreso':
                # Diezmo: solo informativo, no toca ninguna billetera (ver
                # TithesMixin.add_tithe_from_income en src/storage/tithes.py).
                self.add_tithe_from_income(perfil, moneda, monto)
            logger.info(f"Transacción registrada [{perfil}]: {tipo} {moneda}/{cuenta} {monto} - {categoria} - {fecha}")
            return cuenta
        except StorageError:
            self._conn.rollback()
            raise
        except sqlite3.Error as e:
            self._conn.rollback()
            raise StorageError(f"Error al guardar transacción: {e}")

    def peek_last_transaction(self, perfil: str) -> dict:
        """Como `delete_last_transaction` pero de solo lectura -- no borra
        nada ni toca la billetera. Se usa para mostrarle al usuario QUÉ se va
        a deshacer antes de tocar nada (ver _pedir_confirmacion_deshacer en
        src/bot/commands.py), porque "la última transacción" puede ser mucho
        más vieja de lo que el usuario espera (ej. si encadena varios
        /deshacer seguidos) y antes se borraba a ciegas sin mostrar aviso
        previo."""
        row = self._conn.execute(
            """SELECT id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha
               FROM transactions WHERE perfil = ? ORDER BY id DESC LIMIT 1""",
            (perfil,)
        ).fetchone()
        if row is None:
            return None
        tx_id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha = row
        return {
            "id": tx_id, "tipo": tipo, "monto": monto, "moneda": moneda, "cuenta": cuenta,
            "categoria": categoria, "descripcion": descripcion, "fecha": fecha,
        }

    def delete_last_transaction(self, perfil: str) -> dict:
        """Deshace el último gasto/ingreso registrado DE ESTE PERFIL: revierte
        el delta en la billetera correspondiente y borra la fila de
        `transactions`. Retorna un dict con los datos de lo borrado (para el
        mensaje de confirmación), o None si el perfil no tiene nada que
        deshacer.

        Nota de alcance (US-002): solo revierte gasto/ingreso, NO ajustes de
        saldo directos (/saldo_inicial, "tengo X en efectivo") ni
        transferencias entre billeteras propias -- esos quedan para una
        futura iteración si hace falta, ver progress.txt."""
        row = self._conn.execute(
            """SELECT id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha
               FROM transactions WHERE perfil = ? ORDER BY id DESC LIMIT 1""",
            (perfil,)
        ).fetchone()
        if row is None:
            return None
        tx_id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha = row
        try:
            delta = -monto if tipo == 'ingreso' else monto  # revierte el delta original
            self._conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? "
                "WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (delta, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            self._conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            self._conn.commit()
            if tipo == 'ingreso':
                self.revert_tithe_from_income(perfil, moneda, monto)
            nuevo_balance = self.get_wallet_balance(perfil, moneda, cuenta)
            logger.info(f"Transacción deshecha [{perfil}]: id={tx_id} {tipo} {moneda}/{cuenta} {monto} - {categoria}")
            return {
                "tipo": tipo, "monto": monto, "moneda": moneda, "cuenta": cuenta,
                "categoria": categoria, "descripcion": descripcion, "fecha": fecha,
                "nuevo_balance": nuevo_balance,
            }
        except sqlite3.Error as e:
            self._conn.rollback()
            raise StorageError(f"Error al deshacer la transacción: {e}")

    def delete_transaction_by_id(self, perfil: str, tx_id: int) -> dict:
        """Como `delete_last_transaction`, pero borra puntualmente la
        transacción `tx_id` (verificando que sea DE ESTE PERFIL) en vez de
        "la que sea la última en ese momento". Se usa tras confirmar con
        botones qué transacción específica se va a deshacer (ver
        peek_last_transaction/_pedir_confirmacion_deshacer) -- así, si entre
        el aviso y la confirmación se registró algo nuevo, no se borra por
        error la transacción equivocada; simplemente ya no coincide y se
        avisa que ya no está disponible."""
        row = self._conn.execute(
            """SELECT id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha
               FROM transactions WHERE id = ? AND perfil = ?""",
            (tx_id, perfil)
        ).fetchone()
        if row is None:
            return None
        tx_id, tipo, monto, moneda, cuenta, categoria, descripcion, fecha = row
        try:
            delta = -monto if tipo == 'ingreso' else monto  # revierte el delta original
            self._conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? "
                "WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (delta, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            self._conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            self._conn.commit()
            if tipo == 'ingreso':
                self.revert_tithe_from_income(perfil, moneda, monto)
            nuevo_balance = self.get_wallet_balance(perfil, moneda, cuenta)
            logger.info(f"Transacción deshecha [{perfil}]: id={tx_id} {tipo} {moneda}/{cuenta} {monto} - {categoria}")
            return {
                "tipo": tipo, "monto": monto, "moneda": moneda, "cuenta": cuenta,
                "categoria": categoria, "descripcion": descripcion, "fecha": fecha,
                "nuevo_balance": nuevo_balance,
            }
        except sqlite3.Error as e:
            self._conn.rollback()
            raise StorageError(f"Error al deshacer la transacción: {e}")

    def _ensure_category(self, categoria: str, perfil: str) -> None:
        """Inserta la categoría dinámica en `categories` (scopeada al perfil)
        si no existe."""
        if categoria == 'Ingreso':
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO categories (name, is_fixed, perfil) VALUES (?, 0, ?)",
            (categoria, perfil)
        )

    def get_all_categories(self, perfil: str) -> list:
        """Retorna las categorías fijas (compartidas) + las dinámicas de este perfil."""
        rows = self._conn.execute(
            "SELECT name FROM categories WHERE perfil = '' OR perfil = ? "
            "ORDER BY is_fixed DESC, name ASC",
            (perfil,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_dynamic_categories(self, perfil: str) -> list:
        rows = self._conn.execute(
            "SELECT name FROM categories WHERE is_fixed = 0 AND perfil = ? ORDER BY name ASC",
            (perfil,)
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Resumen
    # ------------------------------------------------------------------ #

    def get_summary(self, perfil: str, fecha_desde: str = None, fecha_hasta: str = None,
                     moneda: str = None) -> dict:
        """
        Calcula totales y porcentaje de gasto por categoría en un rango de fechas
        (formato YYYY-MM-DD, inclusive), solo para este perfil. Si no se pasan
        fechas, usa todo el historial. Si se pasa `moneda`, filtra por esa
        moneda; si no, retorna un dict por moneda.

        Returns (si se pasa moneda):
            {
                "total_gastos": float,
                "total_ingresos": float,
                "categorias": [{"categoria": str, "total": float, "porcentaje": float}, ...]
            }
        Returns (si NO se pasa moneda):
            { "Bs": {...}, "USD": {...}, "COP": {...} }
        """
        if moneda is None:
            return {m: self.get_summary(perfil, fecha_desde, fecha_hasta, m) for m in MONEDAS_VALIDAS}

        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'

        where = ["moneda = ?", "perfil = ?"]
        params = [moneda, perfil]
        if fecha_desde:
            where.append("fecha >= ?")
            params.append(fecha_desde)
        if fecha_hasta:
            where.append("fecha <= ?")
            params.append(fecha_hasta)
        where_clause = " AND ".join(where)

        total_gastos_row = self._conn.execute(
            f"SELECT COALESCE(SUM(monto), 0) FROM transactions WHERE tipo = 'gasto' AND {where_clause}",
            params
        ).fetchone()
        total_gastos = total_gastos_row[0] if total_gastos_row else 0.0

        total_ingresos_row = self._conn.execute(
            f"SELECT COALESCE(SUM(monto), 0) FROM transactions WHERE tipo = 'ingreso' AND {where_clause}",
            params
        ).fetchone()
        total_ingresos = total_ingresos_row[0] if total_ingresos_row else 0.0

        cat_rows = self._conn.execute(
            f"""SELECT categoria, SUM(monto) as total
                FROM transactions
                WHERE tipo = 'gasto' AND {where_clause}
                GROUP BY categoria
                ORDER BY total DESC""",
            params
        ).fetchall()

        categorias = []
        for categoria, total in cat_rows:
            porcentaje = (total / total_gastos * 100) if total_gastos > 0 else 0.0
            categorias.append({
                "categoria": categoria,
                "total": total,
                "porcentaje": round(porcentaje, 1)
            })

        return {
            "total_gastos": total_gastos,
            "total_ingresos": total_ingresos,
            "categorias": categorias
        }

    # ------------------------------------------------------------------ #
    # Exportación (respaldo manual vía /exportar)
    # ------------------------------------------------------------------ #

    def get_all_transactions(self, perfil: str) -> list:
        """Retorna TODAS las transacciones (gastos e ingresos) de este perfil,
        de más antigua a más reciente, para el respaldo CSV de /exportar.
        No incluye ajustes de saldo directos (`balance_snapshots`) ni
        transferencias entre billeteras propias -- solo movimientos de
        gasto/ingreso, que es lo que la mayoría espera ver en un respaldo."""
        rows = self._conn.execute(
            """SELECT fecha, tipo, monto, moneda, cuenta, categoria, descripcion, created_at
               FROM transactions WHERE perfil = ? ORDER BY fecha ASC, id ASC""",
            (perfil,)
        ).fetchall()
        return [
            {
                "fecha": r[0], "tipo": r[1], "monto": r[2], "moneda": r[3],
                "cuenta": r[4], "categoria": r[5], "descripcion": r[6], "created_at": r[7],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Racha de días registrando (para el recordatorio nocturno de Coco)
    # ------------------------------------------------------------------ #

    def has_transactions_on(self, perfil: str, fecha: str) -> bool:
        """True si este perfil tiene al menos una transacción con esa fecha (YYYY-MM-DD)."""
        row = self._conn.execute(
            "SELECT 1 FROM transactions WHERE perfil = ? AND fecha = ? LIMIT 1", (perfil, fecha)
        ).fetchone()
        return row is not None

    def get_current_streak(self, perfil: str, reference_date=None) -> int:
        """Cuenta días consecutivos (terminando en reference_date, hoy por
        defecto) con al menos una transacción registrada, solo para este perfil."""
        day = reference_date or datetime.now().date()
        if isinstance(day, str):
            day = datetime.strptime(day, "%Y-%m-%d").date()
        rows = self._conn.execute(
            "SELECT DISTINCT fecha FROM transactions WHERE perfil = ? AND fecha <= ?",
            (perfil, day.strftime("%Y-%m-%d"))
        ).fetchall()
        dias_con_registro = {r[0] for r in rows}
        racha = 0
        cursor = day
        while cursor.strftime("%Y-%m-%d") in dias_con_registro:
            racha += 1
            cursor -= timedelta(days=1)
        return racha
