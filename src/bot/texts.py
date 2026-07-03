"""Textos estáticos (o casi-estáticos) del bot: bienvenida/ayuda de /start y
/help. Separados de commands.py para que el contenido largo en prosa no se
mezcle con la lógica de los comandos."""
from src.services import fx


def _welcome_text(nuevo_registro: bool = False) -> str:
    intro = (
        "🐊 Un gusto. Acabo de abrirle su propio libro de cuentas, separado del "
        "de cualquier otra persona que me escriba -- lo que usted me confíe "
        "aquí es solo suyo.\n\n"
        if nuevo_registro else "🐊 Buenas. Aquí su banquero de confianza, a la orden.\n\n"
    )
    return intro + """Cuénteme sus gastos, ingresos o saldos como se los diría a un amigo --
texto, foto de una captura, o nota de voz -- y yo se los anoto.

📝 Ejemplos de gasto/ingreso:
• "Compré pan por 500" (Bs por defecto)
• "Gasté 20 dólares en el super"
• "Pagué 3000 pesos por un almuerzo"
• "Me pagaron el sueldo, 50000"

💰 Ejemplos para declararme cuánto tiene (sin que sea un gasto/ingreso nuevo):
• "Tengo 50 dólares en efectivo"
• "En Binance tengo 200"
• "Me quedan 300 mil bolívares en el BDV"

🔄 Ejemplos para mover dinero entre sus propias billeteras (no es gasto ni ingreso):
• "Moví 50 dólares de Binance a efectivo"
• "Cambié 20 dólares por 3600 pesos y los metí en efectivo"
• "Cambié 100 mil bolívares a dólares en Binance, a 190 el cambio" (me dice la tasa
  y yo le calculo cuánto entra)

También puede enviarme:
📸 Una captura de una transferencia -> la registro como gasto/ingreso
📸 Una captura de su saldo (BDV o Binance) -> actualizo esa billetera directo
🎤 Una nota de voz describiendo el gasto o el saldo

Comandos:
/menu - botones rápidos (saldo, resumen, cambio, ayuda)
/saldo - ver su saldo (BDV, Binance, y Efectivo con USD + COP juntos)
/resumen - ver gastos del mes por categoría (con porcentajes)
/saldo_inicial <monto> [moneda] [cuenta] - configurar el saldo de una billetera
/cuenta_nueva <moneda> <nombre> - abrir una cuenta nueva (ej: otro banco además de BDV)
/cambio - ver tasas BCV y Binance
/exportar - descargar un respaldo CSV de todos sus movimientos
/deshacer - revertir el último gasto o ingreso registrado
/presupuesto [categoría] [monto] - fijar o ver topes mensuales por categoría
/racha - ver sus días seguidos registrando
/diezmo - ver su diezmo pendiente (10% de cada ingreso, solo informativo)
/diezmo_pagado [moneda] - marcar el diezmo como pagado (o dígame "ya pagué el diezmo")
/deudas - ver deudas y préstamos informales pendientes (quién le debe, a quién le debe)
/help - ver categorías y ayuda"""


def _help_text(categories: list) -> str:
    categories_str = '\n• '.join(categories)
    return f"""🐊 Ayuda -- lo que este banquero sabe hacer

📂 Categorías fijas de gasto:
• {categories_str}

(También puedo abrir categorías nuevas solo si el gasto no encaja en ninguna,
ej: "Gasto de Gio", "Comida en la calle".)

👛 Billeteras que administro por usted:
• BDV (Bs) — su cuenta bancaria en bolívares
• Binance (USD) — sus dólares digitales/USDT
• Efectivo — dólares y pesos (COP) en cash, juntos en /saldo con el
  equivalente combinado en cada moneda (tasa fija: {fx.COP_PER_USD:,.0f} COP = 1 USD)
• Si tiene MÁS cuentas (ej. otro banco además de BDV, o Zelle además de
  Binance), le abro una nueva con /cuenta_nueva o, si de una vez me dice
  "tengo X en [nombre de cuenta]" y no la reconozco, le pregunto si quiere
  que la abra antes de guardar nada

📝 Cómo usarme:
Envíeme un mensaje, foto o nota de voz describiendo el gasto, ingreso o saldo, por ejemplo:
• "Compré X por Y" / "Gasté Z en [categoría]" / "Cobré Z de [fuente]"
• "Tengo Z dólares en efectivo" / "En Binance tengo Z" -> actualizo esa billetera directo
• "Moví Z de Binance a efectivo" / "Cambié Z dólares por W pesos" -> transferencia entre
  sus propias billeteras (no cuenta como gasto ni ingreso en /resumen)
Si no menciona moneda, asumo Bs. Puede decir "20 dólares" o "3000 pesos" para USD/COP.
En USD, si no menciona Binance/USDT/cripto, asumo que es Efectivo.

🤝 Deudas y préstamos informales (con personas que NO usan el bot):
• "Le presté 50 dólares a Pedro" / "Maria me prestó 20 mil bolívares" -> queda anotado
• "Pedro me pagó lo que debía" / "Le abone 10 dólares a Pedro" -> aplica el pago (FIFO,
  de la deuda más vieja a la más nueva)
No toca ninguna billetera -- es solo un registro de quién le debe a quién.

Comandos:
/menu - botones rápidos (saldo, resumen, cambio, ayuda), también escribiendo "menu"
/saldo - su saldo (BDV, Binance, Efectivo), con conversión BCV/Binance de sus Bs
/resumen [mes] - resumen y % de gasto por categoría del mes actual (o YYYY-MM)
/saldo_inicial <monto> [moneda] [cuenta] - fija el saldo de una billetera
  (moneda: Bs/USD/COP; cuenta obligatoria si moneda es USD: Binance o Efectivo)
/cuenta_nueva <moneda> <nombre> - abre una cuenta personalizada (ej: /cuenta_nueva Bs Mercantil)
/cambio - tasas BCV, Binance y USD->COP
/exportar - descargar un CSV con todo su historial (respaldo manual)
/deshacer - revierte el último gasto/ingreso, por si algo se registró mal
/presupuesto <categoría> <monto en Bs> - fija un tope mensual; sin argumentos, lo lista
  (le aviso en la confirmación del gasto si va llegando al 80% o ya lo superó)
/racha - sus días seguidos registrando (también sale dentro de /saldo)
/diezmo - cuánto tiene pendiente de diezmo (10% de cada ingreso, se acumula solo;
  nunca toca ninguna billetera ni el saldo de /saldo, es solo informativo)
/diezmo_pagado [Bs/USD/COP] - marca el diezmo como pagado (todo, o solo esa moneda);
  también puede decirme "ya pagué el diezmo" por texto, voz o una captura de la transferencia
/deudas - resumen neto de deudas/préstamos informales por persona y moneda (no toca
  ninguna billetera; dígame "le presté X a [persona]" o "[persona] me pagó" para registrar)"""
