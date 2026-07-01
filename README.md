# Bot de Telegram de finanzas personales

Bot que registra tus gastos e ingresos escribiéndole en lenguaje natural
("Compré pan por 500", "Cobré el sueldo, 50000"), enviándole una foto
(captura de una transferencia o de tu saldo en el BDV/Binance), o una nota
de voz. Trackea tu plata en 4 billeteras independientes (BDV en Bs, Binance
en USD, Efectivo en USD y Efectivo en COP), te muestra qué porcentaje de tu
gasto va a cada categoría, además de un reporte semanal en imagen todos los
domingos a las 8:00 AM.

Construido reutilizando partes ya probadas del proyecto open source
[telegram-bot-gastos-llm](https://github.com/juaiglesias/telegram-bot-gastos-llm)
(cliente Gemini, prompt builder, validadores), reemplazando Google Sheets
por SQLite local y agregando comandos de saldo/resumen con porcentajes.

## Requisitos

- Python 3.9+
- Token de bot de Telegram (vía [@BotFather](https://t.me/BotFather))
- API key de Gemini (gratis en [Google AI Studio](https://aistudio.google.com/))

## Instalación

```bash
cd gastos-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # completar TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ALLOWED_USER_IDS
```

Para encontrar tu `user_id` de Telegram (y restringir el bot solo a vos),
hablale a [@userinfobot](https://t.me/userinfobot).

## Uso

```bash
python run.py
```

Comandos del bot:
- `/start`, `/help`
- Mensaje libre → registra gasto, ingreso o **ajuste de saldo** (ej: "gasté 1200
  en supermercado", "gasté 20 dólares", "pagué 3000 pesos", o "tengo 50
  dólares en efectivo" / "en Binance tengo 200" para declarar cuánto tienes
  sin que sea un movimiento nuevo). Si no especificás moneda, se asume Bs.
- Foto de una captura de pantalla → ver sección "Fotos y capturas de pantalla".
- Nota de voz → se transcribe y registra igual que un mensaje de texto (gasto,
  ingreso o ajuste de saldo).
- `/saldo` → tus 4 billeteras (BDV, Binance, Efectivo USD, Efectivo COP), con
  el equivalente en USD de tu saldo BDV a tasa BCV y Binance.
- `/saldo_inicial <monto> [moneda] [cuenta]` → fija el saldo de una billetera
  puntual. `cuenta` es obligatoria si `moneda` es USD (Binance o Efectivo).
  Ej: `/saldo_inicial 100000` (Bs, BDV), `/saldo_inicial 200 USD Binance`,
  `/saldo_inicial 50 USD Efectivo`.
- `/resumen` o `/resumen 2026-06` → gasto total y % por categoría del mes,
  desglosado por moneda.
- `/cambio` (alias `/tasas`) → tasa BCV, tasa Binance/paralelo y USD→COP.

### Multi-moneda y billeteras

El bot registra transacciones en tres monedas: `Bs` (bolívares, default),
`USD` y `COP` (pesos colombianos, tasa fija 1 USD = 3600 COP). Gemini infiere
la moneda del mensaje (ej. "20 dólares" → USD, "3000 pesos" → COP); si no hay
ninguna pista, asume Bs.

Dentro de eso, el saldo se trackea por **billetera** (`src/storage/db.py`,
tabla `wallets`), no solo por moneda, porque en la práctica son bolsillos que
no se mezclan:

| Moneda | Cuenta     | Qué es                          |
|--------|-----------|----------------------------------|
| Bs     | BDV       | Cuenta bancaria en el Banco de Venezuela |
| USD    | Binance   | USDT / dólares digitales         |
| USD    | Efectivo  | Dólares en cash                  |
| COP    | Efectivo  | Pesos colombianos en cash        |

Cada billetera es un contador independiente: los gastos/ingresos la
incrementan o decrementan, y una foto de saldo o una frase como "tengo 50
dólares en efectivo" la puede **sobrescribir directo** (ver siguiente
sección). Para USD, si el mensaje no menciona Binance/USDT/cripto
explícitamente, Gemini asume `Efectivo` (el caso más común del día a día).

### Tasas de cambio (BCV y Binance)

`src/services/fx.py` consulta la API gratuita de
[pyDolarVenezuela](https://pydolarve.org/api/v1/dollar) para la tasa oficial
BCV y la tasa paralelo/Binance, con cache de 1 hora en la tabla `fx_rates`
de SQLite. Si la API falla, se usa el último valor cacheado (aunque esté
vencido) o, si nunca hubo cache, un valor de respaldo fijo. La forma exacta
de la respuesta de esa API puede cambiar; si eso ocurre, ajustar
`_fetch_rate_from_api` en `src/services/fx.py`.

### Categorías fijas + dinámicas

Además de las categorías fijas (`EXPENSE_CATEGORIES` en `.env`), el bot
puede crear categorías nuevas automáticamente cuando un gasto no encaja en
ninguna fija (ej. "le presté a Gio" → categoría "Gasto de Gio", "comí algo
en la calle" → "Comida en la calle"). Estas categorías se guardan en la
tabla `categories` (`is_fixed=0`) y se le pasan a Gemini en cada mensaje
para que las reutilice en vez de crear variantes parecidas.

### Fotos y capturas de pantalla

El bot analiza la imagen con Gemini Vision y distingue dos casos
(`captura_tipo` en la respuesta del modelo):

- **Transferencia** (confirmación de pago/transferencia): se registra
  automáticamente como gasto o ingreso, igual que un mensaje de texto.
- **Saldo** (pantalla que muestra el balance total de una cuenta, sea BDV o
  Binance): el bot **adopta ese número directo** como el nuevo saldo de la
  billetera correspondiente (`db.set_wallet_balance`), sin pedir
  confirmación — así registrar no tiene fricción. Para que notes al toque si
  el OCR/LLM leyó mal un número, la respuesta siempre muestra el saldo
  anterior y el nuevo, y además queda guardado un registro en la tabla
  `balance_snapshots` por si hace falta auditar o corregir a mano. Gemini
  decide sola qué billetera es según la app de la captura: BDV/Banesco/
  Mercantil/etc. → Bs/BDV, Binance → USD/Binance, cualquier otra app en
  dólares → USD/Efectivo.

### Notas de voz

Se transcriben y parsean en una sola llamada a Gemini (el audio `.ogg` de
Telegram se envía directo al modelo, sin transcripción intermedia por
separado) — es más simple y evita un paso extra que podría fallar. Puede
resultar en un gasto/ingreso o en un ajuste de saldo, igual que el texto.

### Reporte semanal (domingo 8:00 AM)

Cada domingo a las 8:00 AM (hora del servidor) el bot genera una imagen con
el total gastado en Bs de la semana (lunes a domingo) y un gráfico de dona
por categoría, estilo app de finanzas oscura, y se la envía a todos los
`ALLOWED_USER_IDS`. Se implementa con el `JobQueue` de
`python-telegram-bot` (`run_daily(..., days=(6,))`), cuya convención de
día de semana es la misma que `datetime.date.weekday()`: 0=lunes ... 6=domingo.

## Correrlo 24/7 junto a tu otro bot (systemd)

Cada bot debe correr como su propio servicio systemd, así no interfieren entre sí:

```bash
sudo mkdir -p /var/log/gastos-bot
sudo cp systemd/gastos-bot.service.template /etc/systemd/system/gastos-bot.service
sudo nano /etc/systemd/system/gastos-bot.service   # ajustar User, WorkingDirectory, rutas

sudo systemctl daemon-reload
sudo systemctl enable gastos-bot
sudo systemctl start gastos-bot
sudo systemctl status gastos-bot
journalctl -u gastos-bot -f
```

No hace falta tocar el servicio del otro bot: cada uno corre en su propio
proceso, con su propio token y su propio archivo `gastos.db`.

## Estructura

```
gastos-bot/
├── src/
│   ├── main.py                 # entry point
│   ├── config.py
│   ├── bot/telegram_handler.py # comandos y manejo de mensajes
│   ├── llm/
│   │   ├── gemini_client.py    # texto, imagen (Vision) y audio
│   │   └── prompt_builder.py   # prompts texto/imagen/audio, multi-moneda, categorías dinámicas
│   ├── services/fx.py          # tasas BCV/Binance (pyDolarVenezuela) con cache
│   ├── reports/weekly_image.py # imagen del reporte semanal (Pillow)
│   ├── storage/db.py           # SQLite: saldo multi-moneda, transacciones, categorías, fx cache
│   └── utils/                  # validators, exceptions, logger
├── systemd/gastos-bot.service.template
├── .env.example
└── requirements.txt
```

## Notas de seguridad

- Configurá `ALLOWED_USER_IDS` en `.env`; si lo dejás vacío, cualquiera que
  encuentre tu bot puede registrar gastos.
- `gastos.db` y `.env` están en `.gitignore` — no los subas a git.
