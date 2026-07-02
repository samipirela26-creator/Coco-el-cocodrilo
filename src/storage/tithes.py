"""Diezmo (10% de cada ingreso), aislado por perfil. `TithesMixin` se
combina con los demás mixins dentro de `DBClient` (ver src/storage/db.py).

Alcance a propósito (US pedida por Samuel, "función diezmo"): SOLO trackea/
muestra cuánto le corresponde apartar por moneda -- nunca toca ninguna
billetera ni resta saldo disponible en /saldo. Se acumula automáticamente al
registrar un ingreso (10% del monto) y se marca como pagado a mano (comando
/diezmo_pagado, o una frase/captura tipo "ya pagué el diezmo" -- ver
src/llm/prompt_builder.py y src/bot/handlers.py).
"""
import sqlite3
from datetime import datetime
from src.utils.exceptions import StorageError
from src.storage.constants import MONEDAS_VALIDAS

PORCENTAJE_DIEZMO = 0.10


class TithesMixin:
    def add_tithe_from_income(self, perfil: str, moneda: str, monto_ingreso: float) -> None:
        """Suma el 10% de un ingreso recién registrado al acumulado pendiente
        de diezmo de esa moneda. Se llama automáticamente desde
        `TransactionsMixin.append_expense` cuando tipo == 'ingreso'."""
        if moneda not in MONEDAS_VALIDAS:
            return
        aporte = round(float(monto_ingreso) * PORCENTAJE_DIEZMO, 2)
        if aporte <= 0:
            return
        try:
            self._conn.execute(
                """INSERT INTO tithes (perfil, moneda, monto_pendiente, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(perfil, moneda) DO UPDATE SET
                       monto_pendiente = monto_pendiente + excluded.monto_pendiente,
                       updated_at = excluded.updated_at""",
                (perfil, moneda, aporte, datetime.now().isoformat())
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Error al acumular el diezmo: {e}")

    def revert_tithe_from_income(self, perfil: str, moneda: str, monto_ingreso: float) -> None:
        """Contraparte de `add_tithe_from_income`, usada por
        `delete_last_transaction` (/deshacer) cuando se revierte un ingreso:
        resta el 10% de ese ingreso del pendiente, sin bajar de 0 (si el
        usuario ya marcó ese diezmo como pagado, no lo deja en negativo)."""
        if moneda not in MONEDAS_VALIDAS:
            return
        descuento = round(float(monto_ingreso) * PORCENTAJE_DIEZMO, 2)
        if descuento <= 0:
            return
        try:
            self._conn.execute(
                "UPDATE tithes SET monto_pendiente = MAX(0, monto_pendiente - ?), updated_at = ? "
                "WHERE perfil = ? AND moneda = ?",
                (descuento, datetime.now().isoformat(), perfil, moneda)
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Error al revertir el diezmo: {e}")

    def get_tithe_status(self, perfil: str) -> list:
        """Retorna [{"moneda": str, "monto_pendiente": float}, ...] -- solo
        las monedas donde este perfil tiene diezmo pendiente (> 0)."""
        rows = self._conn.execute(
            "SELECT moneda, monto_pendiente FROM tithes WHERE perfil = ? AND monto_pendiente > 0 ORDER BY moneda ASC",
            (perfil,)
        ).fetchall()
        return [{"moneda": m, "monto_pendiente": monto} for m, monto in rows]

    def mark_tithe_paid(self, perfil: str, moneda: str = None) -> list:
        """Marca como pagado el diezmo pendiente: de UNA moneda si se pasa
        `moneda`, o de TODAS las que tengan pendiente > 0 si es None (caso
        más común: "ya pagué el diezmo" sin especificar moneda). Retorna
        [{"moneda", "monto_pagado"}, ...] con lo que se limpió -- lista vacía
        si no había nada pendiente. No toca ninguna billetera."""
        if moneda:
            if moneda not in MONEDAS_VALIDAS:
                return []
            monedas_a_pagar = [moneda]
        else:
            monedas_a_pagar = [p['moneda'] for p in self.get_tithe_status(perfil)]

        pagados = []
        try:
            for m in monedas_a_pagar:
                row = self._conn.execute(
                    "SELECT monto_pendiente FROM tithes WHERE perfil = ? AND moneda = ?",
                    (perfil, m)
                ).fetchone()
                monto_pendiente = row[0] if row else 0.0
                if monto_pendiente <= 0:
                    continue
                self._conn.execute(
                    "UPDATE tithes SET monto_pendiente = 0, updated_at = ? WHERE perfil = ? AND moneda = ?",
                    (datetime.now().isoformat(), perfil, m)
                )
                pagados.append({"moneda": m, "monto_pagado": monto_pendiente})
            self._conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Error al marcar el diezmo como pagado: {e}")
        return pagados
