"""Deudas y préstamos informales entre el usuario y terceros (personas que NO
usan el bot, ej. un amigo o familiar) -- ej. "le presté 50 dólares a Pedro" o
"Maria me prestó 20 mil bolívares". `DebtsMixin` se combina con los demás
mixins dentro de `DBClient` (ver src/storage/db.py).

A propósito NO toca ninguna billetera: prestar/pedir plata informalmente no
es un gasto ni un ingreso del bot (el dinero prestado normalmente ya salió
de la billetera como transferencia/efectivo fuera del sistema, o nunca entró
a una billetera registrada aquí) -- esto es solo un registro de "quién le
debe a quién" para no perder la cuenta, muy común en el día a día venezolano
de prestarse plata entre familia/amigos.
"""
import logging
from datetime import datetime
from src.storage.constants import MONEDAS_VALIDAS

logger = logging.getLogger('gastos-bot')

TIPOS_DEUDA = ('prestado', 'pedido')


class DebtsMixin:
    def register_debt(self, perfil: str, persona: str, tipo: str, moneda: str,
                       monto: float, descripcion: str = '', fecha: str = None) -> int:
        """Registra una deuda nueva DEL PERFIL dado. tipo='prestado' -> la
        persona le queda debiendo a usted; tipo='pedido' -> usted le queda
        debiendo a la persona. Retorna el id de la deuda creada."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        if tipo not in TIPOS_DEUDA:
            tipo = 'prestado'
        persona = (persona or '').strip() or 'Sin nombre'
        fecha = fecha or datetime.now().strftime('%Y-%m-%d')
        cur = self._conn.execute(
            """INSERT INTO debts
               (perfil, persona, tipo, moneda, monto_original, monto_pendiente, descripcion, fecha, saldada, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (perfil, persona, tipo, moneda, monto, monto, descripcion, fecha, datetime.now().isoformat())
        )
        self._conn.commit()
        logger.info(f"Deuda registrada [{perfil}]: {tipo} {moneda} {monto} con {persona}")
        return cur.lastrowid

    def register_payment(self, perfil: str, persona: str, tipo: str, moneda: str,
                          monto: float = 0.0) -> dict:
        """Aplica un abono/pago a las deudas PENDIENTES de esa persona+tipo+
        moneda, de la más antigua a la más nueva (FIFO). Si `monto` es 0 (o
        no se especificó), salda TODO lo pendiente de una vez -- útil cuando
        el usuario dice "Pedro me pagó lo que debía" sin dar un número
        exacto. Retorna cuánto se aplicó, cuánto sobró (si pagó de más de lo
        que había pendiente) y cuántas deudas quedaron saldadas."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        if tipo not in TIPOS_DEUDA:
            tipo = 'prestado'
        persona = (persona or '').strip()
        rows = self._conn.execute(
            "SELECT id, monto_pendiente FROM debts WHERE perfil = ? AND persona = ? "
            "AND tipo = ? AND moneda = ? AND saldada = 0 ORDER BY fecha, id",
            (perfil, persona, tipo, moneda)
        ).fetchall()
        total_pendiente = sum(r[1] for r in rows)
        restante = total_pendiente if not monto else float(monto)

        ahora = datetime.now().isoformat()
        saldadas = 0
        for debt_id, pendiente in rows:
            if restante <= 0:
                break
            aplicado = min(restante, pendiente)
            nuevo_pendiente = pendiente - aplicado
            restante -= aplicado
            if nuevo_pendiente <= 0.005:
                self._conn.execute(
                    "UPDATE debts SET monto_pendiente = 0, saldada = 1, closed_at = ? WHERE id = ?",
                    (ahora, debt_id)
                )
                saldadas += 1
            else:
                self._conn.execute(
                    "UPDATE debts SET monto_pendiente = ? WHERE id = ?", (nuevo_pendiente, debt_id)
                )
        self._conn.commit()
        aplicado_total = (monto if monto else total_pendiente) - restante
        logger.info(
            f"Pago de deuda [{perfil}]: {tipo} {moneda} aplicado={aplicado_total} sobra={restante} "
            f"con {persona} ({saldadas} deuda(s) saldada(s))"
        )
        return {"aplicado": aplicado_total, "sobra": restante, "saldadas": saldadas}

    def list_debts(self, perfil: str, persona: str = None, solo_pendientes: bool = True) -> list:
        """Lista deudas DEL PERFIL dado, opcionalmente filtradas por persona.
        Por defecto solo trae las pendientes (no saldadas)."""
        query = (
            "SELECT persona, tipo, moneda, monto_original, monto_pendiente, "
            "descripcion, fecha, saldada FROM debts WHERE perfil = ?"
        )
        params = [perfil]
        if persona:
            query += " AND persona = ?"
            params.append(persona)
        if solo_pendientes:
            query += " AND saldada = 0"
        query += " ORDER BY persona, fecha"
        rows = self._conn.execute(query, params).fetchall()
        return [
            {"persona": p, "tipo": t, "moneda": m, "monto_original": mo, "monto_pendiente": mp,
             "descripcion": d, "fecha": f, "saldada": bool(s)}
            for p, t, m, mo, mp, d, f, s in rows
        ]

    def resumen_deudas(self, perfil: str) -> dict:
        """Retorna {persona: {moneda: neto}} con todo lo pendiente DEL
        PERFIL, neteando "prestado" (a favor) contra "pedido" (en contra)
        por persona y moneda -- neto positivo significa que esa persona le
        debe a usted; negativo, que usted le debe a ella. Personas/monedas
        ya saldadas del todo (neto ~0) no aparecen."""
        rows = self._conn.execute(
            "SELECT persona, tipo, moneda, SUM(monto_pendiente) FROM debts "
            "WHERE perfil = ? AND saldada = 0 GROUP BY persona, tipo, moneda",
            (perfil,)
        ).fetchall()
        resumen = {}
        for persona, tipo, moneda, total in rows:
            signo = 1 if tipo == 'prestado' else -1
            resumen.setdefault(persona, {})
            resumen[persona][moneda] = resumen[persona].get(moneda, 0.0) + signo * total
        for persona in list(resumen.keys()):
            resumen[persona] = {m: v for m, v in resumen[persona].items() if abs(v) > 0.005}
            if not resumen[persona]:
                del resumen[persona]
        return resumen
