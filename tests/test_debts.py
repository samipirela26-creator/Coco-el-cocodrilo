"""Pruebas de deudas/préstamos informales (persona a persona, NO con el bot)
-- ver DebtsMixin en src/storage/debts.py. A propósito NO tocan ninguna
billetera: son solo un registro de "quién le debe a quién".
"""


def test_register_debt_prestado_queda_pendiente(db):
    debt_id = db.register_debt("juan", "Pedro", "prestado", "USD", 50.0, descripcion="para el bus")
    assert debt_id > 0
    deudas = db.list_debts("juan")
    assert len(deudas) == 1
    assert deudas[0]["persona"] == "Pedro"
    assert deudas[0]["tipo"] == "prestado"
    assert deudas[0]["monto_pendiente"] == 50.0
    assert deudas[0]["saldada"] is False


def test_register_debt_no_toca_ninguna_billetera(db):
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    db.register_debt("juan", "Pedro", "prestado", "USD", 50.0)
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 100.0


def test_register_payment_parcial(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 50.0)
    resultado = db.register_payment("juan", "Pedro", "prestado", "USD", monto=20.0)
    assert resultado["aplicado"] == 20.0
    assert resultado["sobra"] == 0.0
    assert resultado["saldadas"] == 0
    deudas = db.list_debts("juan")
    assert deudas[0]["monto_pendiente"] == 30.0


def test_register_payment_completo_salda_la_deuda(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 50.0)
    resultado = db.register_payment("juan", "Pedro", "prestado", "USD", monto=50.0)
    assert resultado["aplicado"] == 50.0
    assert resultado["saldadas"] == 1
    assert db.list_debts("juan") == []  # ya no aparece entre las pendientes
    todas = db.list_debts("juan", solo_pendientes=False)
    assert todas[0]["saldada"] is True


def test_register_payment_sin_monto_salda_todo(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 30.0)
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    resultado = db.register_payment("juan", "Pedro", "prestado", "USD")
    assert resultado["aplicado"] == 50.0
    assert resultado["saldadas"] == 2
    assert db.list_debts("juan") == []


def test_register_payment_de_mas_reporta_sobra(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    resultado = db.register_payment("juan", "Pedro", "prestado", "USD", monto=35.0)
    assert resultado["aplicado"] == 20.0
    assert resultado["sobra"] == 15.0
    assert resultado["saldadas"] == 1


def test_register_payment_fifo_deuda_mas_vieja_primero(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 10.0, fecha="2026-01-01")
    db.register_debt("juan", "Pedro", "prestado", "USD", 10.0, fecha="2026-02-01")
    resultado = db.register_payment("juan", "Pedro", "prestado", "USD", monto=10.0)
    assert resultado["saldadas"] == 1
    pendientes = db.list_debts("juan")
    assert len(pendientes) == 1
    assert pendientes[0]["fecha"] == "2026-02-01"  # la más vieja se saldó primero


def test_resumen_deudas_netea_prestado_contra_pedido(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 50.0)
    db.register_debt("juan", "Pedro", "pedido", "USD", 20.0)
    resumen = db.resumen_deudas("juan")
    assert resumen == {"Pedro": {"USD": 30.0}}  # Pedro le debe 30 netos a "juan"


def test_resumen_deudas_neto_cero_no_aparece(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    db.register_debt("juan", "Pedro", "pedido", "USD", 20.0)
    resumen = db.resumen_deudas("juan")
    assert resumen == {}


def test_resumen_deudas_separado_por_moneda(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    db.register_debt("juan", "Pedro", "prestado", "Bs", 500.0)
    resumen = db.resumen_deudas("juan")
    assert resumen == {"Pedro": {"USD": 20.0, "Bs": 500.0}}


def test_deudas_aisladas_por_perfil(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    db.register_debt("maria", "Pedro", "prestado", "USD", 999.0)
    assert db.resumen_deudas("juan") == {"Pedro": {"USD": 20.0}}
    assert db.resumen_deudas("maria") == {"Pedro": {"USD": 999.0}}


def test_list_debts_filtra_por_persona(db):
    db.register_debt("juan", "Pedro", "prestado", "USD", 20.0)
    db.register_debt("juan", "Maria", "pedido", "Bs", 100.0)
    solo_pedro = db.list_debts("juan", persona="Pedro")
    assert len(solo_pedro) == 1
    assert solo_pedro[0]["persona"] == "Pedro"
