"""Servicio de tasas de cambio Bs->USD (BCV y paralelo) usando la API gratuita
DolarApi (https://ve.dolarapi.com/v1/dolares) -- reemplazo de pyDolarVenezuela
(pydolarve.org), cuyo dominio dejó de resolver (caído/discontinuado, verificado
desde dos redes distintas el 2026-07-02). DolarApi es de código abierto
(enzonotario/esjs-dolar-api en GitHub), sin API key, y expone endpoints
separados por fuente: /v1/dolares/oficial (BCV) y /v1/dolares/paralelo.

La API exacta puede cambiar de forma (parámetros, nombres de campos), por lo
que este cliente es defensivo: si falla la petición o el parseo, usa el
último valor cacheado en la tabla `fx_rates` de SQLite (hasta 1 hora), y si
tampoco hay cache, devuelve un valor de respaldo fijo (FALLBACK_RATE).

También expone COP_PER_USD, tasa fija (1 USD = 3600 COP) que el usuario pidió
sin API en vivo.
"""
import logging
from datetime import datetime, timedelta
import requests

logger = logging.getLogger('gastos-bot')

BASE_URL = "https://ve.dolarapi.com/v1/dolares"
CACHE_TTL_MINUTES = 60
FALLBACK_RATE = 40.0  # valor de respaldo si nunca hubo cache ni respuesta de la API
COP_PER_USD = 3600.0  # tasa fija pedida por el usuario


def _fetch_rate_from_api(page: str) -> float:
    """Hace la petición HTTP a DolarApi (un fuente/endpoint por tasa) y
    extrae el precio promedio. Puede lanzar excepción si algo falla; el
    llamador debe manejarla."""
    resp = requests.get(f"{BASE_URL}/{page}", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict):
        if "promedio" in data and data["promedio"] is not None:
            return float(data["promedio"])
        # Respaldo por si el campo cambia de nombre: compra/venta promediados.
        compra, venta = data.get("compra"), data.get("venta")
        if compra is not None and venta is not None:
            return (float(compra) + float(venta)) / 2
    raise ValueError(f"No se pudo extraer la tasa de la respuesta: {data}")


def _get_rate_full(db, page: str, cache_key: str) -> tuple:
    """Obtiene una tasa con cache de 1h en la tabla fx_rates. Nunca lanza
    excepción: si todo falla, retorna el fallback (o el último cache aunque
    esté vencido, si existe).

    Retorna (rate, previous_rate_or_None) -- `previous_rate` es el valor que
    HABÍA en cache justo antes de refrescarlo con uno nuevo (para poder avisar
    de variaciones), y es None si no hubo una tasa nueva de por medio (cache
    todavía vigente, o no había nada cacheado antes)."""
    cached_rate, fetched_at = db.get_cached_fx_rate(cache_key)

    if cached_rate is not None and fetched_at:
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
            if datetime.now() - fetched_dt < timedelta(minutes=CACHE_TTL_MINUTES):
                return cached_rate, None
        except ValueError:
            pass

    try:
        rate = _fetch_rate_from_api(page)
        db.set_cached_fx_rate(cache_key, rate)
        return rate, cached_rate
    except Exception as e:
        logger.warning(f"No se pudo obtener tasa '{page}' desde la API: {e}")
        if cached_rate is not None:
            logger.info(f"Usando última tasa cacheada para '{page}': {cached_rate}")
            return cached_rate, None
        logger.warning(f"Sin cache disponible para '{page}', usando fallback {FALLBACK_RATE}")
        return FALLBACK_RATE, None


def _get_rate(db, page: str, cache_key: str) -> float:
    rate, _ = _get_rate_full(db, page, cache_key)
    return rate


def get_bcv_rate(db) -> float:
    """Tasa oficial BCV (Bs por USD)."""
    return _get_rate(db, page="oficial", cache_key="bcv")


def get_binance_rate(db) -> float:
    """Tasa paralelo/Binance (Bs por USD)."""
    return _get_rate(db, page="paralelo", cache_key="binance")


def get_bcv_rate_with_variation(db) -> tuple:
    """Como get_bcv_rate, pero retorna (rate, previous_rate_or_None) para
    poder avisar si la tasa se movió bastante desde la última vez consultada."""
    return _get_rate_full(db, page="oficial", cache_key="bcv")


def get_binance_rate_with_variation(db) -> tuple:
    """Como get_binance_rate, pero retorna (rate, previous_rate_or_None)."""
    return _get_rate_full(db, page="paralelo", cache_key="binance")


def get_all_rates(db) -> dict:
    """Retorna {"bcv": float, "binance": float, "cop_per_usd": float}."""
    return {
        "bcv": get_bcv_rate(db),
        "binance": get_binance_rate(db),
        "cop_per_usd": COP_PER_USD,
    }
