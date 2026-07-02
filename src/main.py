"""Entry point del bot de Telegram. Adaptado de telegram-bot-gastos-llm."""
import datetime as dt
import io
import logging
import random
import signal
import sys
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from src.config import Config
from src.utils.logger import setup_logger
from src.llm.gemini_client import GeminiClient
from src.storage.db import DBClient
from src.reports.weekly_image import render_weekly_report
from src.bot.commands import (
    start_command, help_command, saldo_command, saldo_inicial_command,
    resumen_command, cambio_command, exportar_command, deshacer_command,
    presupuesto_command, racha_command, menu_command, menu_callback,
    resumen_nav_callback, deshacer_callback, diezmo_command, diezmo_pagado_command,
)
from src.bot.handlers import (
    handle_message, handle_photo, handle_voice, error_handler, category_callback,
)

logger = None

# Recordatorio de Coco a las 22:00 (mismo horario que el resumen nocturno de
# Larry, para aprovechar un hábito ya existente) -- SOLO se manda si ese día
# no hubo ningún registro. Varias variantes para no repetir siempre la misma
# frase (personalidad de Coco: humor ligero, nunca forzado).
RECORDATORIOS_NOCTURNOS = [
    "🐊 Buenas noches. No vi ningún movimiento suyo hoy en el libro mayor... "
    "¿de verdad no gastó ni un centavo, o se le quedó algo por contarme?",
    "🐊 Cierro caja por hoy y su cuenta sigue en blanco. Cuénteme aunque sea "
    "un cafecito, así el banco no se queda con dudas.",
    "🐊 Antes de dormir: ¿algo que registrar hoy? No hay prisa, pero no quiero "
    "que se le escape nada del radar.",
]


async def send_nightly_reminder(context) -> None:
    """Job diario 22:00: por cada perfil (persona) que no haya registrado nada
    hoy, Coco le escribe un recordatorio suave a TODAS sus cuentas de Telegram
    -- nunca si ese perfil ya registró algo. Cada perfil es independiente:
    si Samuel ya registró pero Giovanna no, solo Giovanna recibe el aviso."""
    db: DBClient = context.bot_data['db']
    profile_to_user_ids = context.bot_data.get('profile_to_user_ids') or {}
    if not profile_to_user_ids:
        return

    today_str = dt.datetime.now().strftime("%Y-%m-%d")

    for perfil, user_ids in profile_to_user_ids.items():
        if db.has_transactions_on(perfil, today_str):
            continue
        mensaje = random.choice(RECORDATORIOS_NOCTURNOS)
        for user_id in user_ids:
            try:
                await context.bot.send_message(chat_id=user_id, text=mensaje)
            except Exception as e:
                logger.error(f"No se pudo enviar el recordatorio nocturno a {user_id} ({perfil}): {e}")


async def send_weekly_report(context) -> None:
    """Job del domingo 8:00 AM: genera y envía el reporte semanal en imagen
    a cada perfil (persona) con SUS PROPIOS datos, a todas sus cuentas de
    Telegram. Si no hay ningún perfil configurado, no envía nada."""
    db: DBClient = context.bot_data['db']
    profile_to_user_ids = context.bot_data.get('profile_to_user_ids') or {}

    if not profile_to_user_ids:
        logger.warning("No hay perfiles configurados: no se puede enviar el reporte semanal.")
        return

    today = dt.datetime.now()
    monday = today - dt.timedelta(days=today.weekday())
    # El job corre el domingo, así que el rango es la semana que recién terminó
    # (lunes de esta semana hasta hoy, domingo).
    fecha_desde = monday.strftime("%Y-%m-%d")
    fecha_hasta = today.strftime("%Y-%m-%d")

    for perfil, user_ids in profile_to_user_ids.items():
        try:
            summary = db.get_summary(perfil, fecha_desde, fecha_hasta, moneda='Bs')
            image_bytes = render_weekly_report(summary, moneda='Bs', reference=today)
        except Exception as e:
            logger.exception(f"Error generando el reporte semanal de {perfil}: {e}")
            continue

        for user_id in user_ids:
            try:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=io.BytesIO(image_bytes),
                    caption="📊 Su resumen semanal de gastos"
                )
            except Exception as e:
                logger.error(f"No se pudo enviar el reporte semanal a {user_id} ({perfil}): {e}")


def main():
    global logger
    try:
        print("Cargando configuración...")
        config = Config()

        logger = setup_logger(config)
        logger.info("=" * 50)
        logger.info("Iniciando bot de Telegram de finanzas personales")
        logger.info("=" * 50)

        logger.info(f"Inicializando conector Gemini (modelo: {config.gemini_model})...")
        llm_client = GeminiClient(api_key=config.gemini_api_key, model=config.gemini_model,
                                   backup_keys=config.backup_keys)

        logger.info(f"Inicializando base de datos SQLite ({config.db_path})...")
        db = DBClient(
            db_path=config.db_path,
            initial_balance=config.initial_balance,
            fixed_categories=config.expense_categories,
        )

        logger.info("Configurando bot de Telegram...")
        application = Application.builder().token(config.telegram_bot_token).build()

        application.bot_data["llm_connector"] = llm_client
        application.bot_data["db"] = db
        application.bot_data["categories"] = config.expense_categories
        application.bot_data["allowed_user_ids"] = config.allowed_user_ids
        application.bot_data["user_id_to_profile"] = config.user_id_to_profile
        application.bot_data["profile_to_user_ids"] = config.profile_to_user_ids

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("saldo", saldo_command))
        application.add_handler(CommandHandler("saldo_inicial", saldo_inicial_command))
        application.add_handler(CommandHandler("resumen", resumen_command))
        application.add_handler(CommandHandler("cambio", cambio_command))
        application.add_handler(CommandHandler("tasas", cambio_command))
        application.add_handler(CommandHandler("exportar", exportar_command))
        application.add_handler(CommandHandler("deshacer", deshacer_command))
        application.add_handler(CommandHandler("presupuesto", presupuesto_command))
        application.add_handler(CommandHandler("racha", racha_command))
        application.add_handler(CommandHandler("diezmo", diezmo_command))
        application.add_handler(CommandHandler("diezmo_pagado", diezmo_pagado_command))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^coco_menu:"))
        application.add_handler(CallbackQueryHandler(resumen_nav_callback, pattern=r"^coco_resumen:"))
        application.add_handler(CallbackQueryHandler(deshacer_callback, pattern=r"^coco_deshacer:"))
        application.add_handler(CallbackQueryHandler(category_callback, pattern=r"^coco_cat:"))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        )
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(filters.VOICE, handle_voice))

        application.add_error_handler(error_handler)

        # Reporte semanal automático: domingo 8:00 AM.
        # Nota: en python-telegram-bot, JobQueue.run_daily usa la misma
        # convención que datetime.date.weekday() -> 0=lunes ... 6=domingo.
        # Por lo tanto days=(6,) corresponde a domingo.
        if application.job_queue is not None:
            application.job_queue.run_daily(
                send_weekly_report,
                time=dt.time(hour=8, minute=0),
                days=(6,),
                name="reporte_semanal",
            )
            logger.info("Job de reporte semanal (domingo 8:00 AM) programado.")

            application.job_queue.run_daily(
                send_nightly_reminder,
                time=dt.time(hour=22, minute=0),
                days=(0, 1, 2, 3, 4, 5, 6),
                name="recordatorio_nocturno",
            )
            logger.info("Job de recordatorio nocturno (22:00) programado.")
        else:
            logger.warning(
                "JobQueue no disponible (¿falta instalar python-telegram-bot[job-queue]?). "
                "El reporte semanal automático no se activará."
            )

        def signal_handler(sig, frame):
            logger.info("Señal de terminación recibida. Deteniendo bot...")
            db.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        logger.info("Bot iniciado exitosamente. Escuchando mensajes...")
        logger.info(f"Categorías configuradas: {', '.join(config.expense_categories)}")
        if config.allowed_user_ids:
            logger.info(f"Acceso restringido a user_ids: {config.allowed_user_ids}")
            logger.info(f"Perfiles (datos aislados por persona): {config.profile_to_user_ids}")
        else:
            logger.warning("ALLOWED_USER_IDS no configurado: cualquiera puede usar el bot.")

        application.run_polling(
            poll_interval=2.0,
            timeout=30,
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=-1,
            read_timeout=60,
            connect_timeout=30,
        )

    except ValueError as e:
        print(f"Error de configuración: {e}")
        if logger:
            logger.error(f"Error de configuración: {e}")
        sys.exit(1)

    except Exception as e:
        print(f"Error fatal: {e}")
        if logger:
            logger.exception(f"Error fatal al iniciar bot: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
