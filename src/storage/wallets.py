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
        self._ensure_wallets_for_profile(perfil)
        cuenta = self.resolve_cuenta_perfil(perfil, moneda, cuenta)
        row = self._conn.execute(
            "SELECT balance FROM wallets WHERE perfil = ? AND moneda = ? AND cuenta = ?",
            (perfil, moneda, cuenta)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_all_wallets(self, perfil: str) -> list:
        """Retorna TODAS las billeteras del perfil (las 4 fijas + cualquier
        cuenta personalizada creada con /cuenta_nueva o de forma automática):
        [{"moneda": str, "cuenta": str, "balance": float}, ...]."""
        self._ensure_wallets_for_profile(perfil)
        rows = self._conn.execute(
            "SELECT moneda, cuenta, balance FROM wallets WHERE perfil = ? ORDER BY moneda, cuenta",
            (perfil,)
        ).fetchall()
        return [{"moneda": m, "cuenta": c, "balance": b} for m, c, b in rows]

    def get_known_cuentas(self, perfil: str, moneda: str) -> list:
        """Nombres de todas las cuentas que ya existen para este perfil+moneda
        (las fijas y cualquier personalizada). Se usa para decidir si una
        cuenta mencionada en un mensaje es una ya conocida (aunque con
        mayúsculas/espacios distintos) o una realmente nueva."""
        self._ensure_wallets_for_profile(perfil)
        rows = self._conn.execute(
            "SELECT cuenta FROM wallets WHERE perfil = ? AND moneda = ?", (perfil, moneda)
        ).fetchall()
        return [r[0] for r in rows]

    def find_matching_cuenta(self, perfil: str, moneda: str, cuenta: str) -> str:
        """Busca, sin importar mayúsculas/espacios, si `cuenta` ya corresponde
        a una billetera existente de este perfil+moneda (fija o personalizada)
        -- para no crear duplicados por variaciones de tipeo ("mercantil" vs
        "Mercantil"). Retorna el nombre EXACTO guardado si hay match, o None
        si de verdad parece una cuenta nueva."""
        if not cuenta:
            return None
        objetivo = cuenta.strip().lower()
        if not objetivo:
            return None
        for existente in self.get_known_cuentas(perfil, moneda):
            if existente.strip().lower() == objetivo:
                return existente
        return None

    def resolve_cuenta_perfil(self, perfil: str, moneda: str, cuenta: str = None) -> str:
        """Como resolve_cuenta() (src/storage/constants.py), pero primero
        intenta encontrar una cuenta PERSONALIZADA ya existente de este
        perfil que matchee por nombre -- así, una vez creada una cuenta nueva
        (con /cuenta_nueva o confirmada al detectarla en un ajuste de saldo),
        gastos/ingresos/transferencias/ajustes que la mencionen por nombre
        caen en ella en vez de ser forzados a la cuenta fija por defecto de
        esa moneda. Si no hay match, cae al comportamiento fijo de siempre."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        match = self.find_matching_cuenta(perfil, moneda, cuenta) if cuenta else None
        if match:
            return match
        return resolve_cuenta(moneda, cuenta)

    def create_account(self, perfil: str, moneda: str, cuenta: str, saldo_inicial: float = 0.0) -> bool:
        """Crea una billetera personalizada nueva DEL PERFIL dado (ej: una
        segunda cuenta bancaria en Bs, o un tercer bolsillo en USD) -- a
        diferencia de las 4 fijas de WALLETS, estas se crean bajo demanda: de
        forma deliberada con /cuenta_nueva, o automática (con confirmación
        del usuario) cuando menciona una cuenta que Coco no reconoce todavía
        (ver _pedir_confirmacion_ajuste en src/bot/handlers.py). Retorna False
        sin hacer nada si ya existe una cuenta con ese nombre (sin importar
        mayúsculas) para esa moneda."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = (cuenta or "").strip()
        if not cuenta:
            return False
        self._ensure_wallets_for_profile(perfil)
        if self.find_matching_cuenta(perfil, moneda, cuenta):
            return False
        self._conn.execute(
            "INSERT INTO wallets (perfil, moneda, cuenta, balance, updated_at) VALUES (?, ?, ?, ?, ?)",
            (perfil, moneda, cuenta, saldo_inicial, datetime.now().isoformat())
        )
        self._conn.commit()
        logger.info(f"Cuenta nueva creada [{perfil}]: {moneda}/{cuenta} (saldo inicial {saldo_inicial})")
        return True

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
        cuenta = self.resolve_cuenta_perfil(perfil, moneda, cuenta)
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
            # Si el UPDATE pasó pero el INSERT del snapshot falló (o viceversa),
            # no dejar la conexión con cambios a medias pendientes de commit --
            # deshacer todo y que quede como si no hubiera pasado nada.
            self._conn.rollback()
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
        cuenta_origen = self.resolve_cuenta_perfil(perfil, moneda_origen, cuenta_origen)
        cuenta_destino = self.resolve_cuenta_perfil(perfil, moneda_destino, cuenta_destino)
        self._ensure_wallets_for_profile(perfil)
        try:
            anterior_origen = self.get_wallet_balance(perfil, moneda_origen, cuenta_origen)
            misma_billetera = moneda_origen == moneda_destino and cuenta_origen == cuenta_destino

            if misma_billetera:
                # Transferencia a la misma billetera (raro, pero puede pasar si
                # el LLM interpretó mal el mensaje): de verdad NO se toca el
                # saldo -- antes este caso restaba igual el monto_origen sin
                # sumar nada de vuelta, perdiendo dinero en silencio (bug
                # corregido: ahora nuevo_origen/nuevo_destino quedan iguales
                # al saldo actual, sin ningún UPDATE).
                nuevo_origen = anterior_origen
                anterior_destino = anterior_origen
                nuevo_destino = anterior_origen
            else:
                nuevo_origen = anterior_origen - monto_origen
                anterior_destino = self.get_wallet_balance(perfil, moneda_destino, cuenta_destino)
                nuevo_destino = anterior_destino + monto_destino

            ahora = datetime.now().isoformat()
            if not misma_billetera:
                self._conn.execute(
                    "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                    (nuevo_origen, ahora, perfil, moneda_origen, cuenta_origen)
                )
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
            # Una transferencia toca DOS billeteras + dos snapshots -- si algo
            # falla a mitad de camino (ej. tercer statement), sin este rollback
            # podría quedar el origen ya descontado pero el destino sin
            # acreditar, pendiente de un commit posterior no relacionado.
            self._conn.rollback()
            raise StorageError(f"Error al transferir saldo: {e}")
