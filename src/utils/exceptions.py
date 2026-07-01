"""Excepciones customizadas del proyecto. Adaptado de telegram-bot-gastos-llm."""


class GeminiConnectionError(Exception):
    """Error de conexión con Gemini."""
    pass


class GeminiInvalidJSONError(Exception):
    """El JSON devuelto por Gemini es inválido o no se encontró."""
    pass


class InvalidExpenseDataError(Exception):
    """Los datos de la transacción son inválidos o incompletos."""
    pass


class StorageError(Exception):
    """Error al interactuar con la base de datos SQLite."""
    pass
