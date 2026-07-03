"""Billeteras (saldo por moneda + cuenta), aisladas por perfil. `WalletsMixin`
se combina con los demás mixins dentro de `DBClient` (ver src/storage/db.py).
"""
import sqlite3
import logging
from datetime import datetime
from src.utils.exceptions import StorageError
from src.storage.constants import MONEDAS_VALIDAS, WALLETS, resolve_cuenta

logger = logging.getLogger('gastos-bot')


class WalletsMixin:
    def is_new_profile(self, perfil: str) -> bool:
        """True si este perfil nunca ha sido visto (todavía no tiene ninguna
        billetera creada). Se usa para saludar UNA sola vez a un usuario nuevo
        que se registra solo (registro abierto: cualquiera que le escriba a
        Coco por primera vez recibe su propio perfil aislado, sin que haya
        que agregarlo a mano en ALLOWED_USER_IDS/USER_PROFILES). Debe llamarse
        ANTES de cualquier operación que dispare _ensure_wallets_for_profile."""
        row = self._conn.execute(
            "SELECT 1 FROM wallets WHERE perfil = ? LIMIT 1", (perfil,)
        ).fetchone()
        return row is None

    def get_all_perfiles(self) -> list:
        """Todos los perfiles que ya existen (tienen al menos una billetera
        creada) -- incluye tanto los configurados a mano en USER_PROFILES
        como los de registro abierto (perfil = str(user_id), creados solos
        la primera vez que alguien nuevo le escribe al bot). Se usa para que
        los jobs automáticos (recordatorio nocturno, reporte semanal) también
        alcancen a estos últimos, que no están en ningún mapeo fijo del
        .env -- ver src/main.py: _perfiles_con_user_ids."""
        rows = self._conn.execute("SELECT DISTINCT perfil FROM wallets").fetchall()
        return [r[0] for r in rows]

    def _ensure_wallets_for_profile(self, perfil: str) -> None:
        """Crea las 4 billeteras del perfil si aún no existen (saldo 0, salvo
        Bs/BDV que arranca con INITIAL_BALANCE la primera vez que se ve ese
        perfil)."""
        for moneda, cuenta in WALLETS:
            saldo_inicial = self.initial_balance if (moneda, cuenta) == ('Bs', 'BDV') else 0.0
            self._conn.execute(
                "INSERT OR IGNORE INTO wallets (perfil, moneda, cuenta, balance, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (perfil, moneda, cuenta, saldo_inicial, datetime.now().isoformat())
            )
        self._conn.commit()

    def get_wallet_balance(self, perfil: str, moneda: str, cuenta: str = None) -> float:
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        self._ensure_wallets_for_profile(perfil)
        row = self._conn.execute(
            "SELECT balance FROM wallets WHERE perfil = ? AND moneda = ? AND cuenta = ?",
            (perfil, moneda, cuenta)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_all_wallets(self, perfil: str) -> list:
        """Retorna las 4 billeteras del perfil: [{"moneda": str, "cuenta": str, "balance": float}, ...]."""
        self._ensure_wallets_for_profile(perfil)
        rows = self._conn.execute(
            "SELECT moneda, cuenta, balance FROM wallets WHERE perfil = ? ORDER BY moneda, cuenta",
            (perfil,)
        ).fetchall()
        return [{"moneda": m, "cuenta": c, "balance": b} for m, c, b in rows]

    def set_wallet_balance(self, perfil: str, moneda: str, cuenta: str, monto: float,
                            fuente: str = 'comando') -> tuple:
        """Sobrescribe directo el saldo de una billetera DEL PERFIL dado (ej:
        al leer una captura de saldo, o cuando el usuario dice "tengo X en
        efectivo"). No pide confirmación -- por diseño, para que registrar
        sea sin fricción -- pero deja registro en `balance_snapshots`
        (antes/después) para poder auditar o corregir a mano si algo se leyó
        mal.

        Retorna (cuenta_resuelta, saldo_anterior)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        self._ensure_wallets_for_profile(perfil)
        try:
            anterior = self.get_wallet_balance(perfil, moneda, cuenta)
            self._conn.execute(
                "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (monto, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda, cuenta, anterior, monto, fuente, datetime.now().isoformat(), perfil)
            )
            self._conn.commit()
            logger.info(f"Saldo actualizado [{perfil}]: {moneda}/{cuenta} {anterior} -> {monto} (fuente={fuente})")
            return cuenta, anterior
        except sqlite3.Error as e:
            raise StorageError(f"Error al actualizar saldo: {e}")

    def transfer(self, perfil: str, moneda_origen: str, cuenta_origen: str, monto_origen: float,
                 moneda_destino: str, cuenta_destino: str, monto_destino: float,
                 fuente: str = 'texto') -> dict:
        """Mueve dinero entre dos billeteras DEL MISMO PERFIL (ej. Binance ->
        Efectivo, o un cambio de divisa dólares -> pesos). Resta de la
        billetera origen y suma a la de destino -- NO pasa por `transactions`
        (no es un gasto ni un ingreso nuevo, es el mismo dinero cambiando de
        bolsillo), pero deja registro en `balance_snapshots` de ambos lados
        (fuente='transferencia') para poder auditar.

        Retorna un dict con las cuentas resueltas y saldos antes/después de
        ambas billeteras, para armar el mensaje de confirmación."""
        if moneda_origen not in MONEDAS_VALIDAS:
            moneda_origen = 'Bs'
        if moneda_destino not in MONEDAS_VALIDAS:
            moneda_destino = 'Bs'
        cuenta_origen = resolve_cuenta(moneda_origen, cuenta_origen)
        cuenta_destino = resolve_cuenta(moneda_destino, cuenta_destino)
        self._ensure_wallets_for_profile(perfil)
        try:
            anterior_origen = self.get_wallet_balance(perfil, moneda_origen, cuenta_origen)
            nuevo_origen = anterior_origen - monto_origen

            if moneda_origen == moneda_destino and cuenta_origen == cuenta_destino:
                # Transferencia a la misma billetera (raro, pero por si acaso):
                # no tiene sentido restar y sumar lo mismo -- no hace nada.
                anterior_destino = anterior_origen
                nuevo_destino = anterior_destino
            else:
                anterior_destino = self.get_wallet_balance(perfil, moneda_destino, cuenta_destino)
                nuevo_destino = anterior_destino + monto_destino

            ahora = datetime.now().isoformat()
            self._conn.execute(
                "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (nuevo_origen, ahora, perfil, moneda_origen, cuenta_origen)
            )
            if not (moneda_origen == moneda_destino and cuenta_origen == cuenta_destino):
                self._conn.execute(
                    "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                    (nuevo_destino, ahora, perfil, moneda_destino, cuenta_destino)
                )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda_origen, cuenta_origen, anterior_origen, nuevo_origen, fuente, ahora, perfil)
            )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda_destino, cuenta_destino, anterior_destino, nuevo_destino, fuente, ahora, perfil)
            )
            self._conn.commit()
            logger.info(
                f"Transferencia [{perfil}]: {moneda_origen}/{cuenta_origen} {anterior_origen} -> {nuevo_origen} "
                f"| {moneda_destino}/{cuenta_destino} {anterior_destino} -> {nuevo_destino}"
            )
            return {
                "moneda_origen": moneda_origen, "cuenta_origen": cuenta_origen,
                "anterior_origen": anterior_origen, "nuevo_origen": nuevo_origen,
                "moneda_destino": moneda_destino, "cuenta_destino": cuenta_destino,
                "anterior_destino": anterior_destino, "nuevo_destino": nuevo_destino,
            }
        except sqlite3.Error as e:
            raise StorageError(f"Error al transferir saldo: {e}")
