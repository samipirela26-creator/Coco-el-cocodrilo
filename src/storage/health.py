"""Estado interno del bot (clave/valor), usado por el heartbeat de salud.
`HealthMixin` se combina con los demás mixins dentro de `DBClient` (ver
src/storage/db.py). No está aislado por perfil -- es estado del PROCESO, no
de ningún usuario.

Ver main.py:heartbeat_job (escribe el latido en cada ciclo y detecta si el
Updater de python-telegram-bot murió en silencio) y scripts/chequear_salud.py
(lo lee desde afuera, vía un timer de systemd, para avisar por Telegram si el
bot lleva demasiado tiempo sin dar señales de vida) -- mismo patrón que
agenda-bot (Larry)."""
import logging

logger = logging.getLogger('gastos-bot')


class HealthMixin:
    def estado_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def estado_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        self._conn.commit()
