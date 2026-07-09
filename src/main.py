"""Entry point del bot de Telegram. Adaptado de telegram-bot-gastos-llm."""
import datetime as dt
import io
import logging
import os
import random
import signal
import sys
import time
from telegram import Update, BotCommand, BotCommandScopeChat
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from src.config import Config
from src.utils.logger import setup_logger
from src.llm.gemini_client import GeminiClient
from src.storage.db import DBClient
from src.reports.weekly_image import render_weekly_report
from src.services import fx
from src.bot.formatters import _format_rates_block, _rate_variation_pct
from src.bot.commands import (
    start_command, help_command, saldo_command, saldo_inicial_command,
    resumen_command, cambio_command, exportar_command, deshacer_command,
    presupuesto_command, racha_command, menu_command, menu_callback,
    resumen_nav_callback, deshacer_callback, deshacer_confirmacion_callback, diezmo_command, diezmo_pagado_command,
    bloquear_command, bloquear_callback, desbloquear_command, bloqueados_command,
    cuenta_nueva_command, deudas_command, meta_nueva_command, metas_command,
    borrar_todo_command, borrar_todo_confirmacion_callback,
)
from src.bot.handlers import (
    handle_message, handle_photo, handle_voice, error_handler, category_callback, saldo_callback,
    cuenta_nueva_callback, tipo_transferencia_callback, efectivo_categoria_callback,
    deshacer_transferencia_callback, deshacer_transferencia_confirmacion_callback,
)

logger = None

# Recordatorio de Coco a las 22:00 (mismo horario que el resumen nocturno de
# Larry, para aprovechar un hábito ya existente) -- SOLO se manda si ese día
# no hubo ningún registro. Varias variantes para no repetir siempre la misma
# frase (personalidad de Coco: humor ligero, nunca forzado).
def _perfiles_con_user_ids(config_mapping: dict, db: DBClient) -> dict:
    """Combina el mapeo fijo del .env (USER_PROFILES/ALLOWED_USER_IDS,
    calculado una sola vez al arrancar) con los perfiles de registro abierto
    que ya existen en la base de datos pero que nadie agregó a mano -- para
    esos, el nombre del perfil ES su user_id (ver src/config.py:
    Config.__init__ y src/bot/access.py:_perfil_de), así que se puede derivar
    directo sin necesitar guardarlo aparte. Se recalcula en cada corrida de
    los jobs (no se cachea) para que alcance a quien se registró después de
    que el bot arrancó."""
    resultado = dict(config_mapping)
    ya_mapeados = {uid for ids in resultado.values() for uid in ids}
    for perfil in db.get_all_perfiles():
        if perfil in resultado or not perfil.isdigit():
            continue
        uid = int(perfil)
        if uid in ya_mapeados:
            continue
        resultado[perfil] = [uid]
    return resultado


RECORDATORIOS_NOCTURNOS = [
    "🐊 Buenas noches. No vi ningún movimiento suyo hoy en el libro mayor... "
    "¿de verdad no gastó ni un centavo, o se le quedó algo por contarme?",
    "🐊 Cierro caja por hoy y su cuenta sigue en blanco. Cuénteme aunque sea "
    "un cafecito, así el banco no se queda con dudas.",
    "🐊 Antes de dormir: ¿algo que registrar hoy? No hay prisa, pero no quiero "
    "que se le escape nada del radar.",
]


async def run_daily_backup(context) -> None:
    """Job diario 3:00 AM: respaldo local de gastos.db (últimos 14 días),
    mismo patrón que asistente-bot -- ver DBClient.respaldo_diario. Un
    backup que vive en el mismo disco no reemplaza uno offsite, pero ya es
    muchísimo mejor que depender de que alguien se acuerde de hacerlo a mano."""
    db: DBClient = context.bot_data['db']
    try:
        db.respaldo_diario()
    except Exception as e:
        logger.exception(f"Error al hacer el backup diario de la base de datos: {e}")


async def heartbeat_job(context) -> None:
    """Job cada 60s: escribe el "latido" de vida en la base de datos y
    detecta si el Updater interno de python-telegram-bot murió en silencio.

    Caso real que motivó esto (2026-07-09): un NetworkError durante
    get_updates dejó el proceso "activo" para systemd (el resto del event
    loop -- JobQueue incluido -- seguía corriendo bien) pero la tarea de
    polling murió sin que nada más se enterara, así que el bot quedó sordo
    a mensajes nuevos por más de 30 minutos hasta que alguien lo notó a mano
    y reinició el servicio.

    Por eso este job hace dos cosas:
    1. Si `updater.running` es False, el polling está muerto de verdad --
       nos autodestruimos (`os._exit`) para que systemd (Restart=always)
       levante un proceso nuevo en segundos, en vez de esperar a que alguien
       se dé cuenta.
    2. Si sigue vivo, refresca el latido en `bot_state`. Un chequeo externo
       (scripts/chequear_salud.py, disparado por un timer de systemd fuera
       de este proceso) lee ese latido para avisar por Telegram si lleva
       demasiado tiempo sin refrescarse -- cubre el caso más grave en que
       hasta el event loop completo se cuelga y este mismo job deja de
       correr."""
    db: DBClient = context.bot_data['db']
    updater = context.application.updater
    if updater is not None and not updater.running:
        logger.critical(
            "El Updater de python-telegram-bot dejó de escuchar mensajes "
            "(polling muerto). Reiniciando el proceso para que systemd lo "
            "levante de nuevo..."
        )
        os._exit(1)
    db.estado_set('latido', str(time.time()))


async def send_nightly_reminder(context) -> None:
    """Job diario 22:00: por cada perfil (persona) que no haya registrado nada
    hoy, Coco le escribe un recordatorio suave a TODAS sus cuentas de Telegram
    -- nunca si ese perfil ya registró algo. Cada perfil es independiente:
    si Samuel ya registró pero Giovanna no, solo Giovanna recibe el aviso."""
    db: DBClient = context.bot_data['db']
    config_mapping = context.bot_data.get('profile_to_user_ids') or {}
    profile_to_user_ids = _perfiles_con_user_ids(config_mapping, db)
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


async def send_morning_rates(context) -> None:
    """Job diario 7:00 AM: manda dólar BCV, Binance y euro BCV a TODAS las
    cuentas de Telegram de cada perfil registrado -- pedido explícito del
    usuario (2026-07-09). Las tasas no son datos por perfil (son las mismas
    para todos), así que se calculan UNA sola vez y se reusa el mismo mensaje
    para todo el mundo (a diferencia de send_weekly_report, que sí arma un
    resumen distinto por perfil)."""
    db: DBClient = context.bot_data['db']
    config_mapping = context.bot_data.get('profile_to_user_ids') or {}
    profile_to_user_ids = _perfiles_con_user_ids(config_mapping, db)
    if not profile_to_user_ids:
        return

    bcv, bcv_prev = fx.get_bcv_rate_with_variation(db)
    binance, binance_prev = fx.get_binance_rate_with_variation(db)
    eur_bcv, eur_bcv_prev = fx.get_eur_bcv_rate_with_variation(db)
    bcv_var = _rate_variation_pct(bcv, bcv_prev)
    binance_var = _rate_variation_pct(binance, binance_prev)
    eur_bcv_var = _rate_variation_pct(eur_bcv, eur_bcv_prev)
    mensaje = f"☀️ Buenos días. Así amanecieron las tasas:\n\n" \
        f"{_format_rates_block(bcv, binance, bcv_var, binance_var, eur_bcv, eur_bcv_var)}"

    for user_ids in profile_to_user_ids.values():
        for user_id in user_ids:
            try:
                await context.bot.send_message(chat_id=user_id, text=mensaje)
            except Exception as e:
                logger.error(f"No se pudo enviar las tasas matutinas a {user_id}: {e}")


async def send_weekly_report(context) -> None:
    """Job del domingo 8:00 AM: genera y envía el reporte semanal en imagen
    a cada perfil (persona) con SUS PROPIOS datos, a todas sus cuentas de
    Telegram. Si no hay ningún perfil configurado, no envía nada."""
    db: DBClient = context.bot_data['db']
    config_mapping = context.bot_data.get('profile_to_user_ids') or {}
    profile_to_user_ids = _perfiles_con_user_ids(config_mapping, db)

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


# Comandos públicos: visibles para cualquiera en el botón de menú (☰) de
# Telegram, junto al cuadro de texto -- sin esto, Telegram no sabe qué
# comandos tiene el bot y ese menú aparece vacío.
COMANDOS_PUBLICOS = [
    BotCommand("menu", "Ver el menú con botones"),
    BotCommand("saldo", "Ver sus saldos por billetera"),
    BotCommand("resumen", "Resumen del mes (gastos por categoría)"),
    BotCommand("cambio", "Tasas de cambio actuales (BCV/paralelo)"),
    BotCommand("presupuesto", "Fijar o ver topes mensuales por categoría"),
    BotCommand("racha", "Ver sus días seguidos registrando"),
    BotCommand("diezmo", "Ver diezmo pendiente"),
    BotCommand("diezmo_pagado", "Marcar el diezmo como pagado"),
    BotCommand("deudas", "Ver deudas y préstamos pendientes"),
    BotCommand("metas", "Ver progreso de sus metas de ahorro"),
    BotCommand("meta_nueva", "Crear una meta de ahorro nueva"),
    BotCommand("deshacer", "Deshacer su último gasto/ingreso"),
    BotCommand("borrar_todo", "Borrar TODOS sus datos (empezar de cero)"),
    BotCommand("saldo_inicial", "Fijar su saldo inicial en Bs"),
    BotCommand("cuenta_nueva", "Abrir una cuenta nueva (ej: otro banco)"),
    BotCommand("exportar", "Exportar sus movimientos a CSV"),
    BotCommand("help", "Ver la ayuda completa"),
]

# Comandos solo del dueño (bloquear/desbloquear) -- se registran SOLO en su
# chat privado (BotCommandScopeChat), para que no aparezcan en el menú de
# nadie más (ver src/bot/commands.py:_es_dueno, el chequeo real de permiso
# vive ahí -- esto es solo para que el menú no los muestre a otros).
COMANDOS_DUENO = [
    BotCommand("bloquear", "Bloquear a un usuario por su user_id"),
    BotCommand("desbloquear", "Desbloquear a un usuario"),
    BotCommand("bloqueados", "Ver la lista de usuarios bloqueados"),
]


async def _post_init(application) -> None:
    """Registra los comandos con Telegram (setMyCommands) al arrancar, para
    que el botón de menú (☰) de Telegram los muestre. Se corre una sola vez
    al iniciar -- Telegram guarda esta lista del lado suyo, así que no hace
    falta repetirlo en cada mensaje."""
    await application.bot.set_my_commands(COMANDOS_PUBLICOS)
    owner_id = application.bot_data.get("owner_user_id")
    if owner_id:
        try:
            await application.bot.set_my_commands(
                COMANDOS_PUBLICOS + COMANDOS_DUENO,
                scope=BotCommandScopeChat(chat_id=owner_id),
            )
        except Exception as e:
            logger.warning(f"No se pudo registrar el menú de comandos del dueño: {e}")
    logger.info("Menú de comandos (☰) registrado en Telegram.")


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
        application = Application.builder().token(config.telegram_bot_token).post_init(_post_init).build()

        application.bot_data["llm_connector"] = llm_client
        application.bot_data["db"] = db
        application.bot_data["categories"] = config.expense_categories
        application.bot_data["allowed_user_ids"] = config.allowed_user_ids
        application.bot_data["user_id_to_profile"] = config.user_id_to_profile
        application.bot_data["profile_to_user_ids"] = config.profile_to_user_ids
        application.bot_data["owner_user_id"] = config.owner_user_id

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("saldo", saldo_command))
        application.add_handler(CommandHandler("saldo_inicial", saldo_inicial_command))
        application.add_handler(CommandHandler("resumen", resumen_command))
        application.add_handler(CommandHandler("cambio", cambio_command))
        application.add_handler(CommandHandler("tasas", cambio_command))
        application.add_handler(CommandHandler("exportar", exportar_command))
        application.add_handler(CommandHandler("deshacer", deshacer_command))
        application.add_handler(CommandHandler("borrar_todo", borrar_todo_command))
        application.add_handler(CommandHandler("presupuesto", presupuesto_command))
        application.add_handler(CommandHandler("racha", racha_command))
        application.add_handler(CommandHandler("diezmo", diezmo_command))
        application.add_handler(CommandHandler("diezmo_pagado", diezmo_pagado_command))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("bloquear", bloquear_command))
        application.add_handler(CommandHandler("desbloquear", desbloquear_command))
        application.add_handler(CommandHandler("bloqueados", bloqueados_command))
        application.add_handler(CommandHandler("cuenta_nueva", cuenta_nueva_command))
        application.add_handler(CommandHandler("deudas", deudas_command))
        application.add_handler(CommandHandler("metas", metas_command))
        application.add_handler(CommandHandler("meta_nueva", meta_nueva_command))
        application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^coco_menu:"))
        application.add_handler(CallbackQueryHandler(resumen_nav_callback, pattern=r"^coco_resumen:"))
        application.add_handler(CallbackQueryHandler(deshacer_callback, pattern=r"^coco_deshacer:"))
        application.add_handler(CallbackQueryHandler(deshacer_confirmacion_callback, pattern=r"^coco_deshacerconf:"))
        application.add_handler(CallbackQueryHandler(borrar_todo_confirmacion_callback, pattern=r"^coco_borrartodo:"))
        application.add_handler(CallbackQueryHandler(category_callback, pattern=r"^coco_cat:"))
        application.add_handler(CallbackQueryHandler(efectivo_categoria_callback, pattern=r"^coco_efectivo:"))
        application.add_handler(CallbackQueryHandler(bloquear_callback, pattern=r"^coco_bloquear:"))
        application.add_handler(CallbackQueryHandler(saldo_callback, pattern=r"^coco_saldo:"))
        application.add_handler(CallbackQueryHandler(cuenta_nueva_callback, pattern=r"^coco_cuentanueva:"))
        application.add_handler(CallbackQueryHandler(tipo_transferencia_callback, pattern=r"^coco_tipotransf:"))
        application.add_handler(CallbackQueryHandler(deshacer_transferencia_callback, pattern=r"^coco_deshacertransf:"))
        application.add_handler(CallbackQueryHandler(deshacer_transferencia_confirmacion_callback, pattern=r"^coco_deshacertransfconf:"))
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
            application.job_queue.run_repeating(
                heartbeat_job,
                interval=60,
                first=10,
                name="latido_salud",
            )
            logger.info("Job de latido de salud (cada 60s) programado.")

            application.job_queue.run_daily(
                send_morning_rates,
                time=dt.time(hour=7, minute=0),
                days=(0, 1, 2, 3, 4, 5, 6),
                name="tasas_matutinas",
            )
            logger.info("Job de tasas matutinas (7:00 AM) programado.")

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

            application.job_queue.run_daily(
                run_daily_backup,
                time=dt.time(hour=3, minute=0),
                days=(0, 1, 2, 3, 4, 5, 6),
                name="backup_diario",
            )
            logger.info("Job de backup diario de la base de datos (3:00 AM) programado.")
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
