"""Interfaz base para conectores LLM."""
from abc import ABC, abstractmethod


class LLMConnector(ABC):
    """Interfaz común para todos los conectores LLM."""

    @abstractmethod
    def generate(self, prompt: str) -> dict:
        """
        Genera una respuesta a partir de un prompt y retorna JSON parseado.

        Args:
            prompt: Prompt completo para el modelo

        Returns:
            Diccionario con los datos del gasto (monto, categoria, fecha)

        Raises:
            OllamaConnectionError / GeminiConnectionError: Si no se puede conectar
            OllamaInvalidJSONError / GeminiInvalidJSONError: Si no se puede parsear el JSON
        """
