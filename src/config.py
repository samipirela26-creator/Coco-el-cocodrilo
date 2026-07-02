"""Configuración del bot cargada desde variables de entorno.
Adaptado de telegram-bot-gastos-llm: quita Sheets/Ollama, agrega SQLite.
"""
import os
from dotenv import load_dotenv


class Config:
    """Clase de configuración que carga y valida variables de entorno."""

    def __init__(self):
        load_dotenv()

        self.telegram_bot_token = self._get_required_env('TELEGRAM_BOT_TOKEN')

        # Gemini
        self.gemini_api_key = self._get_required_env('GEMINI_API_KEY')
        self.gemini_model = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash')

        # IAs de respaldo gratis (formato OpenAI), por si Gemini agota su cuota.
        # Todas opcionales: las que queden vacías simplemente se saltan.
        self.backup_keys = {
            'GROQ_API_KEY': os.getenv('GROQ_API_KEY', ''),
            'OPENROUTER_API_KEY': os.getenv('OPENROUTER_API_KEY', ''),
            'MISTRAL_API_KEY': os.getenv('MISTRAL_API_KEY', ''),
            'ZHIPU_API_KEY': os.getenv('ZHIPU_API_KEY', ''),
            'XAI_API_KEY': os.getenv('XAI_API_KEY', ''),
        }

        # SQLite
        self.db_path = os.getenv('DB_PATH', 'gastos.db')
        self.initial_balance = float(os.getenv('INITIAL_BALANCE', '0'))

        # Categorías
        categories_str = os.getenv(
            'EXPENSE_CATEGORIES',
            'Supermercado,Salidas,Combustible,Mascotas,Regalos,Delivery,Servicios,Compras,Salud,Deporte,Otros'
        )
        self.expense_categories = [cat.strip() for cat in categories_str.split(',')]

        # Restringir quién puede usar el bot (opcional, recomendado)
        allowed_ids_str = os.getenv('ALLOWED_USER_IDS', '')
        self.allowed_user_ids = [
            int(uid.strip()) for uid in allowed_ids_str.split(',') if uid.strip()
        ]

        # Perfiles: agrupa varias cuentas de Telegram bajo un mismo "perfil"
        # (comparten saldos/gastos/racha, ej. dos cuentas de la misma persona).
        # Formato: "nombre:id1|id2,otro_nombre:id3". Cualquier ID permitido
        # que NO aparezca en ningún grupo es su propio perfil aislado por
        # defecto (nadie más ve sus datos) -- así, por defecto, cada amigo
        # que agregues a ALLOWED_USER_IDS queda separado sin configurar nada más.
        profiles_str = os.getenv('USER_PROFILES', '')
        self.user_id_to_profile = {}
        self.profile_to_user_ids = {}
        for grupo in profiles_str.split(','):
            grupo = grupo.strip()
            if not grupo or ':' not in grupo:
                continue
            nombre, ids_str = grupo.split(':', 1)
            nombre = nombre.strip()
            if not nombre:
                continue
            ids = [int(i.strip()) for i in ids_str.split('|') if i.strip()]
            self.profile_to_user_ids[nombre] = ids
            for uid in ids:
                self.user_id_to_profile[uid] = nombre
        for uid in self.allowed_user_ids:
            if uid not in self.user_id_to_profile:
                nombre = str(uid)
                self.user_id_to_profile[uid] = nombre
                self.profile_to_user_ids.setdefault(nombre, []).append(uid)

        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        self.log_dir = os.getenv('LOG_DIR', 'logs')

    def _get_required_env(self, key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise ValueError(f"Variable de entorno {key} es requerida")
        return value
