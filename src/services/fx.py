"""Servicio de tasas de cambio Bs->USD (BCV y Binance/paralelo) usando la API
gratuita de pyDolarVenezuela (https://pydolarve.org/api/v1/dollar).

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

BASE_URL = "https://pydolarve.org/api/v1/dollar"
CACHE_TTL_MINUTES = 60
FALLBACK_RATE = 40.0  # valor de respaldo si nunca hubo cache ni respuesta de la API
COP_PER_USD = 3600.0  # tasa fija pedida por el usuario


def _fetch_rate_from_api(page: str) -> float:
    """Hace la petición HTTP a pyDolarVenezuela y extrae el precio promedio.
    Puede lanzar excepción si algo falla; el llamador debe manejarla."""
    params = {"page": page}
    resp = requests.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # La forma exacta de la respuesta puede variar; probamos varias rutas comunes.
    if isinstance(data, dict):
        if "monitors" in data and isinstance(data["monitors"], dict):
            # Estructura tipo {"monitors": {"usd": {"price": ...}}}
            for v in data["monitors"].values():
                if isinstance(v, dict) and "price" in v:
                    return float(v["price"])
        if "price" in data:
            return float(data["price"])
        if "promedio" in data:
            return float(data["promedio"])
    raise ValueError(f"No se pudo extraer la tasa de la respuesta: {data}")


def _get_rate(db, page: str, cache_key: str) -> float:
    """Obtiene una tasa con cache de 1h en la tabla fx_rates. Nunca lanza
    excepción: si todo falla, retorna el fallback (o el último cache aunque
    esté vencido, si existe)."""
    cached_rate, fetched_at = db.get_cached_fx_rate(cache_key)

    if cached_rate is not None and fetched_at:
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
            if datetime.now() - fetched_dt < timedelta(minutes=CACHE_TTL_MINUTES):
                return cached_rate
        except ValueError:
            pass

    try:
        rate = _fetch_rate_from_api(page)
        db.set_cached_fx_rate(cache_key, rate)
        return rate
    except Exception as e:
        logger.warning(f"No se pudo obtener tasa '{page}' desde la API: {e}")
        if cached_rate is not None:
            logger.info(f"Usando última tasa cacheada para '{page}': {cached_rate}")
            return cached_rate
        logger.warning(f"Sin cache disponible para '{page}', usando fallback {FALLBACK_RATE}")
        return FALLBACK_RATE


def get_bcv_rate(db) -> float:
    """Tasa oficial BCV (Bs por USD)."""
    return _get_rate(db, page="bcv", cache_key="bcv")


def get_binance_rate(db) -> float:
    """Tasa paralelo/Binance (Bs por USD)."""
    return _get_rate(db, page="enparalelovzla", cache_key="binance")


def get_all_rates(db) -> dict:
    """Retorna {"bcv": float, "binance": float, "cop_per_usd": float}."""
    return {
        "bcv": get_bcv_rate(db),
        "binance": get_binance_rate(db),
        "cop_per_usd": COP_PER_USD,
    }
