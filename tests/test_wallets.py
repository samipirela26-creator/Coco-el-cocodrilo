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


def test_create_account_agrega_cuenta_personalizada(db):
    creada = db.create_account("juan", "Bs", "Mercantil", 300.0)
    assert creada is True
    assert db.get_wallet_balance("juan", "Bs", "Mercantil") == 300.0
    cuentas = {(w["moneda"], w["cuenta"]) for w in db.get_all_wallets("juan")}
    assert ("Bs", "Mercantil") in cuentas


def test_create_account_no_duplica_por_mayusculas(db):
    db.create_account("juan", "Bs", "Mercantil", 100.0)
    creada_de_nuevo = db.create_account("juan", "Bs", "mercantil", 999.0)
    assert creada_de_nuevo is False
    # El saldo original no debe haberse tocado -- el segundo intento no hizo nada.
    assert db.get_wallet_balance("juan", "Bs", "Mercantil") == 100.0


def test_find_matching_cuenta_ignora_mayusculas_y_espacios(db):
    db.create_account("juan", "USD", "Zelle", 50.0)
    assert db.find_matching_cuenta("juan", "USD", "  zelle ") == "Zelle"
    assert db.find_matching_cuenta("juan", "USD", "Otra") is None


def test_resolve_cuenta_perfil_usa_cuenta_personalizada_existente(db):
    db.create_account("juan", "Bs", "Mercantil", 0.0)
    db.set_wallet_balance("juan", "Bs", "mercantil", 250.0)  # distinta capitalización
    assert db.get_wallet_balance("juan", "Bs", "Mercantil") == 250.0


def test_resolve_cuenta_perfil_sin_match_cae_al_default_fijo(db):
    # "Provincial" no existe todavia como cuenta -> debe caer a BDV (default de Bs),
    # igual que el comportamiento clásico de resolve_cuenta().
    cuenta_resuelta = db.resolve_cuenta_perfil("juan", "Bs", "Provincial")
    assert cuenta_resuelta == "BDV"
