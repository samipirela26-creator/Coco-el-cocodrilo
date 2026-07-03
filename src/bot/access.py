"""Control de acceso y resolución de perfil para el bot de Telegram.

`_is_allowed` decide si un user_id puede usar el bot (ALLOWED_USER_IDS).
`_check_cooldown` frena ráfagas de mensajes de una misma persona (protege la
cuota de Gemini ahora que el bot es de acceso abierto -- ver COOLDOWN_SECONDS).
`_perfil_de` resuelve a qué "perfil" (persona, datos aislados) pertenece un
user_id de Telegram -- ver src/config.py (USER_PROFILES) y el docstring de
src/storage/db.py para el detalle del aislamiento multi-perfil.
"""
import logging
import time
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger('gastos-bot')

# Mínimo de segundos entre mensajes que gastan una llamada a Gemini (texto,
# foto o nota de voz) de una misma persona. Solo ráfagas -- no hay tope diario
# a propósito (decisión explícita: se prefiere simple, sin bloquear a nadie
# que use el bot con normalidad).
COOLDOWN_SECONDS = 3


def _check_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True si esta persona puede seguir (ya pasó el cooldown desde su último
    mensaje que gastó IA); False si hay que frenarla en silencio. Guarda el
    estado en bot_data (en memoria -- se resetea si el bot reinicia, sin
    problema ya que es solo anti-ráfaga, no un contador persistente)."""
    user_id = update.effective_user.id
    ultimos = context.bot_data.setdefault('ultimo_mensaje_ia', {})
    ahora = time.monotonic()
    anterior = ultimos.get(user_id)
    if anterior is not None and (ahora - anterior) < COOLDOWN_SECONDS:
        return False
    ultimos[user_id] = ahora
    return True


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed = context.bot_data.get('allowed_user_ids') or []
    if not allowed:
        return True
    user_id = update.effective_user.id
    if user_id in allowed:
        return True
    logger.warning(f"Acceso bloqueado: user_id {user_id} no está en ALLOWED_USER_IDS")
    return False


def _perfil_de(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Resuelve a qué 'perfil' (persona) pertenece este user_id de Telegram --
    cada perfil tiene sus propios saldos/gastos/racha, totalmente aislados de
    los demás (ver src/config.py: USER_PROFILES). Si el user_id no está
    agrupado explícitamente, es su propio perfil aislado por defecto."""
    mapping = context.bot_data.get('user_id_to_profile') or {}
    return mapping.get(user_id, str(user_id))
