"""Esquema y migraciones de la base de datos SQLite. `SchemaMixin` se combina
con los demás mixins (wallets, transactions, budgets, fx_cache) dentro de
`DBClient` (ver src/storage/db.py) -- asume que quien lo compone ya tiene
`self._conn` (sqlite3.Connection) abierto.
"""
import sqlite3
import logging
from src.storage.constants import LEGACY_PROFILE

logger = logging.getLogger('gastos-bot')


class SchemaMixin:
    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    def _init_schema(self, fixed_categories: list) -> None:
        cur = self._conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL CHECK(tipo IN ('gasto', 'ingreso')),
                monto REAL NOT NULL,
                categoria TEXT NOT NULL,
                fecha TEXT NOT NULL,
                descripcion TEXT,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                moneda TEXT NOT NULL DEFAULT 'Bs',
                cuenta TEXT NOT NULL DEFAULT 'BDV',
                perfil TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migraciones seguras para bases de datos creadas con un esquema previo.
        for statement in (
            "ALTER TABLE transactions ADD COLUMN moneda TEXT NOT NULL DEFAULT 'Bs'",
            "ALTER TABLE transactions ADD COLUMN cuenta TEXT NOT NULL DEFAULT 'BDV'",
            "ALTER TABLE transactions ADD COLUMN perfil TEXT NOT NULL DEFAULT ''",
        ):
            try:
                cur.execute(statement)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # la columna ya existe
        # Filas viejas (de antes del aislamiento por perfil) quedan asignadas
        # al perfil legacy, en vez de con perfil vacío (que no pertenecería a nadie).
        cur.execute("UPDATE transactions SET perfil = ? WHERE perfil = ''", (LEGACY_PROFILE,))

        self._migrate_categories_table(cur)
        for cat in fixed_categories:
            cur.execute(
                "INSERT OR IGNORE INTO categories (name, is_fixed, perfil) VALUES (?, 1, '')",
                (cat,)
            )
            cur.execute(
                "UPDATE categories SET is_fixed = 1 WHERE name = ? AND perfil = ''", (cat,)
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                page TEXT PRIMARY KEY,
                rate REAL NOT NULL,
                fetched_at TEXT NOT NULL
            )
        """)

        # Presupuestos mensuales por categoría (en Bs, la moneda del día a día),
        # aislados por perfil. Solo un límite activo por (perfil, categoria) --
        # fijar uno nuevo reemplaza al anterior.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                perfil TEXT NOT NULL,
                categoria TEXT NOT NULL,
                limite_mensual REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (perfil, categoria)
            )
        """)

        # Diezmo (10% de cada ingreso), solo informativo -- acumula cuánto le
        # corresponde apartar por moneda, aislado por perfil. NO toca ninguna
        # billetera: se actualiza sumando desde TransactionsMixin.append_expense
        # (cuando tipo == 'ingreso') y se resetea a 0 al marcarlo pagado.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tithes (
                perfil TEXT NOT NULL,
                moneda TEXT NOT NULL,
                monto_pendiente REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (perfil, moneda)
            )
        """)

        # Historial de cambios de saldo por billetera (fotos, comandos, texto/voz
        # tipo "tengo X en efectivo"), para poder auditar o corregir a mano si un
        # OCR/LLM lee mal un número.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moneda TEXT NOT NULL,
                cuenta TEXT NOT NULL,
                monto_anterior REAL NOT NULL,
                monto_nuevo REAL NOT NULL,
                fuente TEXT NOT NULL,
                created_at TEXT NOT NULL,
                perfil TEXT NOT NULL DEFAULT ''
            )
        """)
        try:
            cur.execute("ALTER TABLE balance_snapshots ADD COLUMN perfil TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        cur.execute("UPDATE balance_snapshots SET perfil = ? WHERE perfil = ''", (LEGACY_PROFILE,))

        # Últimos datos conocidos de cada user_id de Telegram (nombre visible,
        # perfil al que pertenece) -- se actualiza en cada mensaje/comando
        # (ver AccessControlMixin.track_user, llamado desde src/bot/access.py).
        # Sirve para que /bloquear pueda mostrar el nombre de la persona antes
        # de bloquear, en vez de bloquear a ciegas por user_id (fácil de
        # equivocarse con un número).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY,
                nombre TEXT,
                perfil TEXT,
                last_seen TEXT NOT NULL
            )
        """)

        # Bloqueo manual de usuarios (ver /bloquear, /desbloquear en
        # src/bot/commands.py) -- solo el dueño del bot (OWNER_USER_ID) puede
        # bloquear/desbloquear. Un user_id bloqueado es rechazado en
        # silencio por _is_allowed (src/bot/access.py), sin importar si
        # ALLOWED_USER_IDS está vacío (acceso abierto) o no.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                nombre TEXT,
                blocked_at TEXT NOT NULL,
                blocked_by INTEGER
            )
        """)

        # Deudas y préstamos informales (persona a persona, NO con el propio
        # bot ni entre perfiles) -- ej. "le presté 50 dólares a Pedro" o
        # "Maria me prestó 20 mil bolívares". A diferencia de gasto/ingreso,
        # NO toca ninguna billetera: es solo un registro de "quién le debe a
        # quién" para no perder la cuenta. tipo='prestado' -> la persona le
        # debe a usted; tipo='pedido' -> usted le debe a la persona. Ver
        # DebtsMixin en src/storage/debts.py.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                perfil TEXT NOT NULL,
                persona TEXT NOT NULL,
                tipo TEXT NOT NULL CHECK(tipo IN ('prestado', 'pedido')),
                moneda TEXT NOT NULL,
                monto_original REAL NOT NULL,
                monto_pendiente REAL NOT NULL,
                descripcion TEXT,
                fecha TEXT NOT NULL,
                saldada INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
        """)

        # Metas de ahorro (ej. "quiero ahorrar 500 dólares para un viaje"),
        # aisladas por perfil. A diferencia de gasto/ingreso, NO toca ninguna
        # billetera: es solo un contador de progreso hacia un objetivo. Ver
        # SavingsMixin en src/storage/savings.py.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                perfil TEXT NOT NULL,
                nombre TEXT NOT NULL,
                moneda TEXT NOT NULL,
                monto_objetivo REAL NOT NULL,
                monto_actual REAL NOT NULL DEFAULT 0,
                cumplida INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        # Marca de "ya se le explicó a este perfil cómo funciona la
        # confirmación con botones (Salida/Entrada) de las capturas de Pago
        # Móvil" -- para mandar esa explicación una sola vez por perfil (ver
        # AccessControlMixin.has_seen_pago_movil_intro / mark_pago_movil_intro_seen
        # y _pedir_confirmacion_tipo_transferencia en src/bot/handlers.py).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pago_movil_intro (
                perfil TEXT PRIMARY KEY,
                shown_at TEXT NOT NULL
            )
        """)

        # wallets: la clave primaria cambia (ahora incluye perfil), así que si
        # la tabla existe con el esquema viejo (sin perfil) hay que migrarla
        # copiando los datos al perfil legacy en vez de solo agregar la columna.
        self._migrate_wallets_table(cur)

        self._conn.commit()

    def _migrate_wallets_table(self, cur) -> None:
        existing = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wallets'"
        ).fetchone()
        needs_migration = existing is not None and not self._column_exists('wallets', 'perfil')

        if needs_migration:
            # Esquema viejo: PRIMARY KEY (moneda, cuenta), sin perfil. SQLite no
            # permite alterar la PK con ALTER TABLE, así que hay que renombrar,
            # crear la tabla nueva, copiar los datos (asignados al perfil
            # legacy) y borrar la vieja.
            cur.execute("ALTER TABLE wallets RENAME TO wallets_old")
            cur.execute("""
                CREATE TABLE wallets (
                    perfil TEXT NOT NULL,
                    moneda TEXT NOT NULL,
                    cuenta TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (perfil, moneda, cuenta)
                )
            """)
            cur.execute(
                "INSERT INTO wallets (perfil, moneda, cuenta, balance, updated_at) "
                "SELECT ?, moneda, cuenta, balance, updated_at FROM wallets_old",
                (LEGACY_PROFILE,)
            )
            cur.execute("DROP TABLE wallets_old")
            self._conn.commit()
            logger.info(f"Migración: tabla 'wallets' migrada al perfil legacy '{LEGACY_PROFILE}'")
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    perfil TEXT NOT NULL,
                    moneda TEXT NOT NULL,
                    cuenta TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (perfil, moneda, cuenta)
                )
            """)

    def _migrate_categories_table(self, cur) -> None:
        existing = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='categories'"
        ).fetchone()
        needs_migration = existing is not None and not self._column_exists('categories', 'perfil')

        if needs_migration:
            # Esquema viejo: PRIMARY KEY (name), sin perfil, con is_fixed
            # distinguiendo fijas de dinámicas. SQLite no permite alterar la
            # PK con ALTER TABLE, así que hay que renombrar, crear la tabla
            # nueva y copiar los datos: las fijas (is_fixed=1) se quedan
            # compartidas (perfil=''), las dinámicas (is_fixed=0) se asignan
            # al perfil legacy (eran de Samuel, único usuario antes de esto).
            cur.execute("ALTER TABLE categories RENAME TO categories_old")
            cur.execute("""
                CREATE TABLE categories (
                    name TEXT NOT NULL,
                    is_fixed INTEGER NOT NULL DEFAULT 0,
                    perfil TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (name, perfil)
                )
            """)
            cur.execute(
                "INSERT INTO categories (name, is_fixed, perfil) "
                "SELECT name, is_fixed, CASE WHEN is_fixed = 1 THEN '' ELSE ? END FROM categories_old",
                (LEGACY_PROFILE,)
            )
            cur.execute("DROP TABLE categories_old")
            self._conn.commit()
            logger.info(f"Migración: tabla 'categories' migrada (dinámicas -> perfil legacy '{LEGACY_PROFILE}')")
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT NOT NULL,
                    is_fixed INTEGER NOT NULL DEFAULT 0,
                    perfil TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (name, perfil)
                )
            """)
