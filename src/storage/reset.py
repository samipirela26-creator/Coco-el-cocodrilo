"""Borrado total de los datos de UN perfil (reset, como si empezara de
cero). `ResetMixin` se combina con los demás mixins dentro de `DBClient`
(ver src/storage/db.py). Pensado para /borrar_todo en src/bot/commands.py,
que pide confirmación con botones antes de llamar a esto -- esta clase NO
pregunta nada, solo ejecuta.
"""
import sqlite3
import logging
from src.utils.exceptions import StorageError

logger = logging.getLogger('gastos-bot')


class ResetMixin:
    def reset_profile(self, perfil: str) -> None:
        """Borra TODOS los datos financieros de este perfil: transacciones,
        billeteras, presupuestos, diezmo pendiente, historial de ajustes de
        saldo, deudas, metas de ahorro, categorías dinámicas propias y la
        marca de "ya vio la explicación de Pago Móvil" (para que si vuelve a
        usar el bot, se lo vuelva a explicar). NO toca `known_users` (nombre/
        perfil visto la última vez, es solo metadato de Telegram) ni
        `blocked_users`/`fx_rates` (globales, no son de ningún perfil).

        Es IRREVERSIBLE -- no hay backup automático acá, la confirmación con
        botones debe pasar ANTES de llamar a esto (ver _pedir_confirmacion_
        borrar_todo en src/bot/commands.py)."""
        try:
            self._conn.execute("DELETE FROM transactions WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM wallets WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM budgets WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM tithes WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM balance_snapshots WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM debts WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM savings_goals WHERE perfil = ?", (perfil,))
            self._conn.execute("DELETE FROM pago_movil_intro WHERE perfil = ?", (perfil,))
            # Solo categorías dinámicas propias del perfil -- las fijas viven
            # con perfil='' (compartidas) y no deben tocarse.
            self._conn.execute("DELETE FROM categories WHERE perfil = ? AND is_fixed = 0", (perfil,))
            self._conn.commit()
            # Recrea las billeteras vacías (BDV/Binance/Efectivo en 0), igual
            # que un perfil recién estrenado -- ver WalletsMixin._ensure_wallets_for_profile.
            self._ensure_wallets_for_profile(perfil)
            logger.info(f"Reset completo de datos para perfil '{perfil}'")
        except sqlite3.Error as e:
            self._conn.rollback()
            raise StorageError(f"Error al borrar los datos: {e}")
