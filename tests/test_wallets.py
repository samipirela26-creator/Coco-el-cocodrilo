"""Pruebas de billeteras: ajuste directo de saldo (fotos/"tengo X en
efectivo") y transferencias entre billeteras propias -- la otra mitad de la
aritmética financiera del bot, junto con test_transactions.py.
"""


def test_set_wallet_balance_sobrescribe_y_retorna_anterior(db):
    cuenta, anterior = db.set_wallet_balance("juan", "USD", "Efectivo", 500.0, fuente="foto")
    assert cuenta == "Efectivo"
    assert anterior == 0.0
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 500.0


def test_set_wallet_balance_dos_veces_seguidas(db):
    db.set_wallet_balance("juan", "Bs", "BDV", 2000.0)
    _, anterior = db.set_wallet_balance("juan", "Bs", "BDV", 1500.0)
    assert anterior == 2000.0
    assert db.get_wallet_balance("juan", "Bs", "BDV") == 1500.0


def test_transfer_mueve_dinero_entre_billeteras_propias(db):
    db.set_wallet_balance("juan", "USD", "Binance", 100.0)
    resultado = db.transfer(
        perfil="juan",
        moneda_origen="USD", cuenta_origen="Binance", monto_origen=40.0,
        moneda_destino="USD", cuenta_destino="Efectivo", monto_destino=40.0,
    )
    assert resultado["nuevo_origen"] == 60.0
    assert resultado["nuevo_destino"] == 40.0
    assert db.get_wallet_balance("juan", "USD", "Binance") == 60.0
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 40.0


def test_transfer_con_cambio_de_moneda_usa_montos_distintos(db):
    # Ej: cambiar 10 USD a Bs a una tasa de 65 -> se acreditan 650 Bs, no 10.
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    resultado = db.transfer(
        perfil="juan",
        moneda_origen="USD", cuenta_origen="Efectivo", monto_origen=10.0,
        moneda_destino="Bs", cuenta_destino="BDV", monto_destino=650.0,
    )
    assert resultado["nuevo_origen"] == 90.0
    assert db.get_wallet_balance("juan", "Bs", "BDV") == 1000.0 + 650.0


def test_transfer_misma_billetera_no_hace_nada(db):
    """Transferir a la misma billetera (raro, pero puede pasar si el LLM se
    equivoca) no debe duplicar ni perder dinero -- el saldo debe quedar
    exactamente igual (bug real encontrado y corregido: antes restaba el
    monto_origen sin acreditar nada de vuelta)."""
    saldo_inicial = db.get_wallet_balance("juan", "Bs", "BDV")
    resultado = db.transfer(
        perfil="juan",
        moneda_origen="Bs", cuenta_origen="BDV", monto_origen=50.0,
        moneda_destino="Bs", cuenta_destino="BDV", monto_destino=50.0,
    )
    assert resultado["nuevo_origen"] == saldo_inicial
    assert resultado["nuevo_destino"] == saldo_inicial
    assert db.get_wallet_balance("juan", "Bs", "BDV") == saldo_inicial


def test_perfiles_tienen_billeteras_independientes(db):
    db.set_wallet_balance("juan", "COP", "Efectivo", 50000.0)
    assert db.get_wallet_balance("maria", "COP", "Efectivo") == 0.0
