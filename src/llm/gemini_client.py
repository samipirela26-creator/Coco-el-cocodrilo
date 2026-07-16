"""Cliente para interactuar con Gemini, con varios modelos y varias IAs de
respaldo por si Gemini se satura. Usa solo `urllib` (sin SDK) para evitar la
dependencia deprecada `google-generativeai` y para poder reutilizar el mismo
patrón de resiliencia que el otro bot (asistente-bot / Larry la Rana)."""
import base64
import json
import re
import socket
import logging
import urllib.request
import urllib.error

from src.llm.base import LLMConnector
from src.llm.prompt_builder import build_image_prompt, build_audio_prompt
from src.utils.exceptions import GeminiConnectionError, GeminiInvalidJSONError


logger = logging.getLogger('gastos-bot')

# Se prueban en orden; si uno esta saturado (429/500/503) se pasa al siguiente.
MODELOS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite",
           "gemini-3.1-flash-lite"]

# IAs de respaldo gratis (formato OpenAI) si TODOS los modelos de Gemini
# fallan. Solo se usan para texto (no imagen/audio). Se saltan solas si no
# hay clave configurada. Sacar clave gratis en:
#   Groq:       https://console.groq.com/keys
#   OpenRouter: https://openrouter.ai/keys
#   Mistral:    https://console.mistral.ai/api-keys
#   Zhipu:      https://open.bigmodel.cn/usercenter/apikeys
#   xAI:        https://console.x.ai
RESPALDOS = [
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions",
     "llama-3.3-70b-versatile"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions",
     "meta-llama/llama-3.3-70b-instruct:free"),
    ("MISTRAL_API_KEY", "https://api.mistral.ai/v1/chat/completions",
     "mistral-small-latest"),
    ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions",
     "glm-4.5-flash"),
    ("XAI_API_KEY", "https://api.x.ai/v1/chat/completions",
     "grok-3-mini"),
]


def _url(modelo: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{modelo}:generateContent"
    )


class GeminiClient(LLMConnector):
    """Cliente para generar respuestas usando Gemini, con respaldos."""

    def __init__(self, api_key: str, model: str = 'gemini-3.5-flash',
                 backup_keys: dict = None):
        """
        Inicializa el cliente de Gemini.

        Args:
            api_key: API key de Google Gemini
            model: Modelo preferido; si se satura se prueban los de MODELOS
            backup_keys: dict {NOMBRE_ENV: clave} de IAs de respaldo (opcional)
        """
        self.api_key = api_key
        self.model_name = model
        self.backup_keys = backup_keys or {}
        logger.info(f"Cliente Gemini inicializado (modelo principal: {model})")

    def _modelos_a_probar(self):
        """El modelo configurado primero, luego el resto de MODELOS sin repetir."""
        vistos = {self.model_name}
        yield self.model_name
        for modelo in MODELOS:
            if modelo not in vistos:
                vistos.add(modelo)
                yield modelo

    def _llamar_gemini(self, cuerpo: dict) -> str:
        """Prueba los modelos de Gemini en orden y devuelve el texto crudo."""
        datos = json.dumps(cuerpo).encode('utf-8')
        ultimo_error = None
        for modelo in self._modelos_a_probar():
            req = urllib.request.Request(
                _url(modelo) + f"?key={self.api_key}",
                data=datos,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    out = json.loads(resp.read().decode('utf-8'))
                return out["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                ultimo_error = e
                if e.code in (429, 500, 503):
                    logger.warning(f"Modelo {modelo} saturado ({e.code}), probando siguiente")
                    continue
                raise
            except (socket.timeout, urllib.error.URLError, TimeoutError) as e:
                ultimo_error = e
                continue
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                ultimo_error = e
                continue
        raise ultimo_error or RuntimeError("No se pudo contactar a ningún modelo de Gemini")

    def _llamar_respaldo(self, prompt_text: str) -> str:
        """Prueba las IAs de respaldo en orden. Lanza el último error si ninguna responde."""
        ultimo = None
        for env_var, url, modelo in RESPALDOS:
            key = self.backup_keys.get(env_var, '').strip()
            if not key:
                continue
            cuerpo = {
                "model": modelo,
                "messages": [{"role": "user", "content": prompt_text}],
                "temperature": 0.2,
                "max_tokens": 1024,
            }
            if "bigmodel.cn" in url:
                cuerpo["thinking"] = {"type": "disabled"}
            req = urllib.request.Request(
                url, data=json.dumps(cuerpo).encode('utf-8'),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    out = json.loads(resp.read().decode('utf-8'))
                logger.info(f"IA de respaldo en uso: {modelo}")
                return out["choices"][0]["message"]["content"]
            except Exception as e:
                ultimo = e
                continue
        raise ultimo or RuntimeError("Sin IAs de respaldo configuradas")

    def generate(self, prompt: str) -> dict:
        """
        Genera una respuesta usando Gemini (con respaldos si falla) y extrae el JSON.

        Args:
            prompt: Prompt completo para el modelo

        Returns:
            Diccionario con los datos del gasto (monto, categoria, fecha)

        Raises:
            GeminiConnectionError: Si no se pudo conectar ni con Gemini ni respaldos
            GeminiInvalidJSONError: Si no se puede parsear el JSON
        """
        cuerpo = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        try:
            logger.debug(f"Enviando prompt a Gemini (modelo: {self.model_name})")
            response_text = self._llamar_gemini(cuerpo)
        except Exception as e:
            logger.warning(f"Gemini no respondió, probando IAs de respaldo: {e}")
            try:
                response_text = self._llamar_respaldo(prompt)
            except Exception as e2:
                logger.error(f"Error al conectar con Gemini y respaldos: {e2}")
                raise GeminiConnectionError(
                    f"No se pudo conectar con Gemini ni con las IAs de respaldo: {e2}")
        logger.debug(f"Respuesta del modelo: {response_text}")
        return self._extract_json_from_text(response_text)

    def analyze_image(self, image_bytes: bytes, categories: list,
                       dynamic_categories: list = None, mime_type: str = "image/jpeg",
                       caption: str = None) -> dict:
        """
        Analiza una captura de pantalla (transferencia o saldo bancario) usando
        Gemini Vision, probando los modelos de MODELOS en orden si alguno se satura.

        Args:
            image_bytes: Bytes crudos de la imagen (jpg/png)
            categories: Categorías fijas
            dynamic_categories: Categorías dinámicas ya existentes
            mime_type: Tipo MIME de la imagen

        Returns:
            Diccionario con "captura_tipo" ("transferencia" o "saldo") y los
            demás campos de la transacción.

        Raises:
            GeminiConnectionError, GeminiInvalidJSONError
        """
        prompt = build_image_prompt(categories, dynamic_categories, caption=caption)
        cuerpo = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode('ascii'),
                    }},
                ],
            }],
        }
        try:
            logger.debug("Enviando imagen a Gemini Vision")
            response_text = self._llamar_gemini(cuerpo)
        except (GeminiInvalidJSONError,):
            raise
        except Exception as e:
            logger.error(f"Error al analizar imagen con Gemini: {e}")
            raise GeminiConnectionError(f"No se pudo analizar la imagen con Gemini: {e}")
        logger.debug(f"Respuesta de Gemini (imagen): {response_text}")
        return self._extract_json_from_text(response_text)

    def transcribe_and_parse_audio(self, audio_bytes: bytes, categories: list,
                                    dynamic_categories: list = None,
                                    mime_type: str = "audio/ogg") -> dict:
        """
        Transcribe y parsea una nota de voz en un solo paso (más simple y robusto
        que hacer dos llamadas separadas: transcribir y luego parsear texto).
        Gemini soporta audio inline (base64) directamente. Prueba los modelos
        de MODELOS en orden si alguno se satura.

        Args:
            audio_bytes: Bytes del archivo de audio (.ogg/opus de Telegram)
            categories: Categorías fijas
            dynamic_categories: Categorías dinámicas ya existentes
            mime_type: Tipo MIME del audio (Telegram voice = audio/ogg)

        Returns:
            Diccionario con los datos de la transacción (mismo esquema que generate).

        Raises:
            GeminiConnectionError, GeminiInvalidJSONError
        """
        prompt = build_audio_prompt(categories, dynamic_categories)
        cuerpo = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(audio_bytes).decode('ascii'),
                    }},
                ],
            }],
        }
        try:
            logger.debug("Enviando audio a Gemini")
            response_text = self._llamar_gemini(cuerpo)
        except (GeminiInvalidJSONError,):
            raise
        except Exception as e:
            logger.error(f"Error al transcribir/parsear audio con Gemini: {e}")
            raise GeminiConnectionError(f"No se pudo procesar la nota de voz con Gemini: {e}")
        logger.debug(f"Respuesta de Gemini (audio): {response_text}")
        return self._extract_json_from_text(response_text)

    def _extract_json_from_text(self, text: str) -> dict:
        """
        Extrae JSON de un texto, incluso si viene rodeado de texto adicional.

        Args:
            text: Texto que contiene JSON

        Returns:
            Diccionario parseado del JSON

        Raises:
            GeminiInvalidJSONError: Si no se encuentra o no se puede parsear el JSON
        """
        json_match = re.search(r'\{.*\}', text, re.DOTALL)

        if not json_match:
            logger.error(f"No se encontró JSON en la respuesta: {text}")
            raise GeminiInvalidJSONError("No se encontró JSON en la respuesta del modelo")

        json_str = json_match.group()

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Error al parsear JSON: {e}")
            raise GeminiInvalidJSONError(f"JSON inválido: {e}")
