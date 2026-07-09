#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Healthcheck de Coco (gastos-bot). Lo corre un timer de systemd cada pocos
minutos, FUERA del proceso del bot -- así detecta hasta el caso en que el
proceso entero se cuelga (event loop completo trabado), no solo cuando el
polling de Telegram muere solo (ese caso ya se autorecupera desde adentro,
ver main.py:heartbeat_job).

El bot escribe un "latido" (timestamp) en `bot_state` en cada ciclo del job
de heartbeat (cada 60s, ver main.py). Si ese latido lleva más de X minutos
sin refrescarse, el bot está caído o colgado de verdad: este script avisa
por Telegram a las cuentas del dueño (una sola vez por incidente, vía la
bandera `salud_alertada` en la misma tabla) y vuelve a avisar cuando se
recupera.

Mismo patrón que agenda-bot (Larry): ver
/home/samuel/asistente/src/chequear_salud.py.

No depende de src/config.py ni src/storage/db.py a propósito -- son
deliberadamente livianos y sin las validaciones de esos módulos (que exigen
GEMINI_API_KEY, etc., innecesario para un simple chequeo de salud), así este
script sigue funcionando aunque el resto del bot esté roto.
"""
import logging
import os
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv

log = logging.getLogger("gastos-bot.salud")

UMBRAL_MIN_DEFECTO = 10  # minutos sin latido = se considera caído


def _enviar_mensaje(texto: str, token: str, chat_id: int) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def _estado_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _estado_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")

    umbral_min = UMBRAL_MIN_DEFECTO
    if len(sys.argv) > 1:
        try:
            umbral_min = float(sys.argv[1])
        except ValueError:
            pass
    umbral = umbral_min * 60

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(base_dir, ".env"))

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    db_path = os.getenv("DB_PATH", "gastos.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(base_dir, db_path)

    chat_ids = [
        int(uid.strip())
        for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]

    if not token or not chat_ids:
        log.error("Falta TELEGRAM_BOT_TOKEN o ALLOWED_USER_IDS; no puedo avisar.")
        return

    if not os.path.exists(db_path):
        log.warning("Aún no existe la base de datos (%s). Nada que reportar.", db_path)
        return

    conn = sqlite3.connect(db_path)
    try:
        # La tabla bot_state solo existe si el bot ya corrió al menos una vez
        # con este código (se crea en SchemaMixin._init_schema).
        existe = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_state'"
        ).fetchone()
        if not existe:
            log.info("Todavía no hay tabla bot_state (bot recién instalado). Nada que reportar.")
            return

        latido = _estado_get(conn, "latido")
        ahora = time.time()
        if not latido:
            log.info("Aún no hay latido (bot recién instalado/reiniciado). Nada que reportar.")
            return

        atraso = ahora - float(latido)
        caido = atraso > umbral
        ya_avisado = _estado_get(conn, "salud_alertada") == "1"

        if caido and not ya_avisado:
            mins = int(atraso / 60)
            msg = (
                f"🐊🛑 <b>Coco no responde</b>\n"
                f"Lleva ~{mins} min sin dar señales de vida (último latido "
                f"hace {mins} min). Revisa el servicio en el Lenovo:\n"
                f"<code>systemctl --user status gastos-bot.service</code>"
            )
            for cid in chat_ids:
                try:
                    _enviar_mensaje(msg, token, cid)
                except Exception as e:
                    log.warning("No se pudo avisar a %s: %s", cid, e)
            _estado_set(conn, "salud_alertada", "1")
            log.info("Bot caído (atraso=%.0fs). Alerta enviada.", atraso)
        elif not caido and ya_avisado:
            for cid in chat_ids:
                try:
                    _enviar_mensaje(
                        "🐊✅ <b>Coco se recuperó</b> y vuelve a responder.",
                        token, cid,
                    )
                except Exception as e:
                    log.warning("No se pudo avisar a %s: %s", cid, e)
            _estado_set(conn, "salud_alertada", "0")
            log.info("Bot recuperado (atraso=%.0fs). Alerta de recuperación enviada.", atraso)
        else:
            log.info("Bot OK (último latido hace %.0fs).", atraso)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
