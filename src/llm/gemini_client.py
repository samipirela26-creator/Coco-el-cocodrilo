"""Cliente para interactuar con Gemini usando la librería oficial de Google."""
import io
import json
import re
import logging
import google.generativeai as genai
from src.llm.base import LLMConnector
from src.llm.prompt_builder import build_image_prompt, build_audio_prompt
from src.utils.exceptions import GeminiConnectionError, GeminiInvalidJSONError


logger = logging.getLogger('gastos-bot')


class GeminiClient(LLMConnector):
    """Cliente para generar respuestas usando Gemini."""

    def __init__(self, api_key: str, model: str = 'gemini-2.0-flash'):
        """
        Inicializa el cliente de Gemini.

        Args:
            api_key: API key de Google Gemini
            model: Nombre del modelo (ej: "gemini-2.0-flash")
        """
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)
        self.model_name = model
        logger.info(f"Cliente Gemini inicializado con modelo: {model}")

    def generate(self, prompt: str) -> dict:
        """
        Genera una respuesta usando Gemini y extrae el JSON.

        Args:
            prompt: Prompt completo para el modelo

        Returns:
            Diccionario con los datos del gasto (monto, categoria, fecha)

        Raises:
            GeminiConnectionError: Si no se puede conectar con Gemini
            GeminiInvalidJSONError: Si no se puede parsear el JSON
        """
        try:
            logger.debug(f"Enviando prompt a Gemini (modelo: {self.model_name})")
            response = self.model.generate_content(prompt)
            response_text = response.text
            logger.debug(f"Respuesta de Gemini: {response_text}")
            return self._extract_json_from_text(response_text)

        except Exception as e:
            logger.error(f"Error al conectar con Gemini: {e}")
            raise GeminiConnectionError(f"No se pudo conectar con Gemini: {e}")

    def analyze_image(self, image_bytes: bytes, categories: list,
                       dynamic_categories: list = None, mime_type: str = "image/jpeg") -> dict:
        """
        Analiza una captura de pantalla (transferencia o saldo bancario) usando
        Gemini Vision, pasando los bytes de la imagen directamente.

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
        try:
            prompt = build_image_prompt(categories, dynamic_categories)
            image_part = {"mime_type": mime_type, "data": image_bytes}
            logger.debug("Enviando imagen a Gemini Vision")
            response = self.model.generate_content([prompt, image_part])
            response_text = response.text
            logger.debug(f"Respuesta de Gemini (imagen): {response_text}")
            return self._extract_json_from_text(response_text)
        except (GeminiInvalidJSONError,):
            raise
        except Exception as e:
            logger.error(f"Error al analizar imagen con Gemini: {e}")
            raise GeminiConnectionError(f"No se pudo analizar la imagen con Gemini: {e}")

    def transcribe_and_parse_audio(self, audio_bytes: bytes, categories: list,
                                    dynamic_categories: list = None,
                                    mime_type: str = "audio/ogg") -> dict:
        """
        Transcribe y parsea una nota de voz en un solo paso (más simple y robusto
        que hacer dos llamadas separadas: transcribir y luego parsear texto).
        Gemini soporta audio inline (bytes) directamente en generate_content.

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
        try:
            prompt = build_audio_prompt(categories, dynamic_categories)
            audio_part = {"mime_type": mime_type, "data": audio_bytes}
            logger.debug("Enviando audio a Gemini")
            response = self.model.generate_content([prompt, audio_part])
            response_text = response.text
            logger.debug(f"Respuesta de Gemini (audio): {response_text}")
            return self._extract_json_from_text(response_text)
        except (GeminiInvalidJSONError,):
            raise
        except Exception as e:
            logger.error(f"Error al transcribir/parsear audio con Gemini: {e}")
            raise GeminiConnectionError(f"No se pudo procesar la nota de voz con Gemini: {e}")

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
