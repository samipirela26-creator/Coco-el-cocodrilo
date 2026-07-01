"""Constructor de prompts para el LLM con contexto temporal.

Adaptado de telegram-bot-gastos-llm: ahora distingue entre gasto e ingreso
para poder calcular saldo, además de monto/categoría/fecha/descripción.
Ahora soporta también: multi-moneda (Bs/USD/COP) y categorías dinámicas.
"""
from datetime import datetime


def _shared_rules(categories_str: str, dynamic_categories_str: str) -> str:
    dynamic_hint = ""
    if dynamic_categories_str:
        dynamic_hint = f"""
Categorías dinámicas ya creadas anteriormente (además de las fijas de arriba),
reutilízalas si el gasto encaja en alguna de ellas en vez de crear una nueva
parecida: "{dynamic_categories_str}"."""

    return f"""
Reglas:
- "tipo" es "gasto" si el usuario pagó/compró/gastó algo, o "ingreso" si recibió/cobró/le pagaron.
- Si "tipo" es "gasto":
  * Si el gasto encaja claramente en una de estas categorías fijas, usa EXACTAMENTE una de ellas: "{categories_str}".
  * Si NO encaja en ninguna fija, puedes proponer una categoría NUEVA, corta y reutilizable
    (ej: si el mensaje menciona a una persona como "Giovanna" o "Gio", usa algo como "Gasto de Gio";
    si es "comida en la calle" usa "Comida en la calle").{dynamic_hint}
  * Antes de proponer una categoría nueva, revisa si ya existe una parecida (fija o dinámica) y reutilízala.
- Si "tipo" es "ingreso", usa "categoria": "Ingreso".
- "descripcion" es un breve resumen del gasto/ingreso (qué se compró, de dónde vino el ingreso, etc).
- Si no hay fecha explícita, asume hoy.
- "monto" siempre debe ser un número positivo.
- "moneda" debe ser una de: "Bs", "USD", "COP".
  * Si el usuario menciona dólares, "$", "dolares" o "USD" -> "USD".
  * Si el usuario menciona pesos o "COP" -> "COP".
  * Si no hay ninguna indicación de moneda, asume "Bs" (bolívares), que es el caso más común.
"""


def build_prompt(user_message: str, categories: list, dynamic_categories: list = None) -> str:
    """
    Construye el prompt completo con fecha actual y mensaje del usuario.

    IMPORTANTE: Inyecta la fecha actual para que el modelo pueda interpretar
    referencias temporales como "ayer", "anteayer", "la semana pasada", etc.

    Args:
        user_message: Mensaje del usuario sobre el gasto o ingreso
        categories: Lista de categorías fijas válidas (para gastos)
        dynamic_categories: Lista de categorías dinámicas ya creadas previamente

    Returns:
        Prompt completo listo para enviar al modelo
    """
    today = datetime.now().strftime("%Y-%m-%d")
    categories_str = '", "'.join(categories)
    dynamic_categories_str = '", "'.join(dynamic_categories) if dynamic_categories else ""

    system_prompt = f"""Eres un asistente contable personal.
HOY ES {today}.

Tu única función es recibir frases sobre gastos o ingresos de dinero y responder
EXCLUSIVAMENTE con un objeto JSON.

Formato: {{"tipo": <"gasto" o "ingreso">, "monto": <float, siempre positivo>, "categoria": <string>, "moneda": <"Bs", "USD" o "COP">, "fecha": <string formato Y-m-d>, "descripcion": <string>}}
{_shared_rules(categories_str, dynamic_categories_str)}
IMPORTANTE: Responde SOLO con el JSON, sin texto adicional, sin markdown."""

    return f"{system_prompt}\n\nUsuario: {user_message}\n\nAsistente:"


def build_image_prompt(categories: list, dynamic_categories: list = None) -> str:
    """
    Prompt para analizar una captura de pantalla (transferencia o saldo bancario).

    Returns:
        Prompt completo para enviar junto con la imagen a Gemini Vision.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    categories_str = '", "'.join(categories)
    dynamic_categories_str = '", "'.join(dynamic_categories) if dynamic_categories else ""

    system_prompt = f"""Eres un asistente contable personal que analiza capturas de pantalla
de aplicaciones bancarias o de pago (ej. Banesco, Mercantil, BDV, Binance, Zelle, Pago Móvil).
HOY ES {today}.

Existen dos tipos de captura posibles:
1. "transferencia": una confirmación de pago/transferencia (envío o recepción de dinero).
2. "saldo": una pantalla que muestra el saldo/balance total de una cuenta (no un movimiento).

Responde EXCLUSIVAMENTE con un objeto JSON con este formato:
{{"captura_tipo": <"transferencia" o "saldo">, "tipo": <"gasto" o "ingreso", solo si captura_tipo es "transferencia">, "monto": <float, siempre positivo>, "categoria": <string, solo si captura_tipo es "transferencia">, "moneda": <"Bs", "USD" o "COP">, "fecha": <string formato Y-m-d>, "descripcion": <string>}}
{_shared_rules(categories_str, dynamic_categories_str)}
Reglas adicionales:
- Si es una confirmación de transferencia donde el usuario ENVÍA dinero (paga algo, transfiere a otra persona/comercio), "tipo" es "gasto".
- Si es una confirmación donde el usuario RECIBE dinero, "tipo" es "ingreso".
- Si es una pantalla de saldo de cuenta (no un movimiento), usa "captura_tipo": "saldo" y en "monto" pon el saldo mostrado.
  En este caso "tipo", "categoria" y "descripcion" pueden omitirse o dejarse vacíos.
- Si no hay fecha visible, asume hoy.

IMPORTANTE: Responde SOLO con el JSON, sin texto adicional, sin markdown."""

    return system_prompt


def build_audio_prompt(categories: list, dynamic_categories: list = None) -> str:
    """
    Prompt para transcribir y parsear una nota de voz en un solo paso.

    Returns:
        Prompt completo para enviar junto con el audio a Gemini.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    categories_str = '", "'.join(categories)
    dynamic_categories_str = '", "'.join(dynamic_categories) if dynamic_categories else ""

    system_prompt = f"""Eres un asistente contable personal. Vas a recibir una nota de voz en español
donde el usuario describe un gasto o ingreso de dinero. Escucha el audio, entiende lo que dice,
y responde EXCLUSIVAMENTE con un objeto JSON.
HOY ES {today}.

Formato: {{"tipo": <"gasto" o "ingreso">, "monto": <float, siempre positivo>, "categoria": <string>, "moneda": <"Bs", "USD" o "COP">, "fecha": <string formato Y-m-d>, "descripcion": <string>}}
{_shared_rules(categories_str, dynamic_categories_str)}
IMPORTANTE: Responde SOLO con el JSON, sin texto adicional, sin markdown."""

    return system_prompt
