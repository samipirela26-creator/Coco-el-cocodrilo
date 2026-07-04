"""Bloqueo manual de usuarios y registro de "quién es quién". `AccessControlMixin`
se combina con los demás mixins dentro de `DBClient` (ver src/storage/db.py).

Pensado para el escenario de acceso abierto (cualquiera puede escribirle al
bot, ver ALLOWED_USER_IDS vacío en Config): si alguien molesto empieza a usar
el bot, el dueño necesita poder bloquearlo sin tener que entrar por SSH.
`track_user` guarda el último nombre visible de cada user_id (se llama en
cada mensaje, ver src/bot/access.py:_is_allowed) para que /bloquear pueda
mostrar un nombre y no solo un número -- bloquear al user_id equivocado por
error tipográfico sería fácil si solo se mostrara el ID.
"""
import logging
from datetime import datetime

logger = logging.getLogger('gastos-bot')


class AccessControlMixin:
    def track_user(self, user_id: int, nombre: str, perfil: str) -> None:
        """Actualiza (o crea) el registro de "última vez visto" de este
        user_id -- se llama en cada mensaje/comando, así que siempre queda al
        día el nombre más reciente que Telegram nos dio."""
        self._conn.execute(
            """INSERT INTO known_users (user_id, nombre, perfil, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   nombre = excluded.nombre, perfil = excluded.perfil, last_seen = excluded.last_seen""",
            (user_id, nombre, perfil, datetime.now().isoformat())
        )
        self._conn.commit()

    def get_known_user(self, user_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT user_id, nombre, perfil, last_seen FROM known_users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "nombre": row[1], "perfil": row[2], "last_seen": row[3]}

    def is_blocked(self, user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    def block_user(self, user_id: int, nombre: str, blocked_by: int) -> None:
        self._conn.execute(
            """INSERT INTO blocked_users (user_id, nombre, blocked_at, blocked_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   nombre = excluded.nombre, blocked_at = excluded.blocked_at, blocked_by = excluded.blocked_by""",
            (user_id, nombre, datetime.now().isoformat(), blocked_by)
        )
        self._conn.commit()
        logger.info(f"Usuario bloqueado: {user_id} ({nombre}) por {blocked_by}")

    def unblock_user(self, user_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_blocked(self) -> list:
        rows = self._conn.execute(
            "SELECT user_id, nombre, blocked_at FROM blocked_users ORDER BY blocked_at DESC"
        ).fetchall()
        return [{"user_id": r[0], "nombre": r[1], "blocked_at": r[2]} for r in rows]

    def has_seen_pago_movil_intro(self, perfil: str) -> bool:
        """True si ya se le explicó a este perfil, alguna vez, cómo funciona
        la confirmación con botones (Salida/Entrada) de las capturas de Pago
        Móvil -- para mandar esa explicación solo la primera vez (ver
        _pedir_confirmacion_tipo_transferencia en src/bot/handlers.py)."""
        row = self._conn.execute(
            "SELECT 1 FROM pago_movil_intro WHERE perfil = ?", (perfil,)
        ).fetchone()
        return row is not None

    def mark_pago_movil_intro_seen(self, perfil: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO pago_movil_intro (perfil, shown_at) VALUES (?, ?)",
            (perfil, datetime.now().isoformat())
        )
        self._conn.commit()
