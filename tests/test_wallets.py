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


def test_peek_last_transfer_sin_transferencias_retorna_none(db):
    # Un ajuste de saldo (set_wallet_balance) escribe UN solo snapshot, no dos
    # con el mismo timestamp -- no debe confundirse con una transferencia.
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    assert db.peek_last_transfer("juan") is None


def test_peek_last_transfer_identifica_la_ultima_transferencia(db):
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    db.transfer(
        perfil="juan",
        moneda_origen="USD", cuenta_origen="Efectivo", monto_origen=40.0,
        moneda_destino="USD", cuenta_destino="Binance", monto_destino=40.0,
    )
    pendiente = db.peek_last_transfer("juan")
    assert pendiente is not None
    assert pendiente["moneda_origen"] == "USD" and pendiente["cuenta_origen"] == "Efectivo"
    assert pendiente["anterior_origen"] == 100.0 and pendiente["nuevo_origen"] == 60.0
    assert pendiente["moneda_destino"] == "USD" and pendiente["cuenta_destino"] == "Binance"
    assert pendiente["anterior_destino"] == 0.0 and pendiente["nuevo_destino"] == 40.0
    # Mismas claves que el resultado de transfer() -- reusable por los mismos formatters.
    assert set(pendiente) >= {"anterior_origen", "nuevo_origen", "anterior_destino", "nuevo_destino"}


def test_peek_last_transfer_no_confunde_con_gasto_posterior(db):
    # Si después de la transferencia hay un gasto/ingreso normal (que no
    # escribe balance_snapshots), la transferencia debe seguir siendo "la
    # última" detectable -- pero si hay OTRO ajuste de saldo suelto después,
    # ya no debe emparejarse con la transferencia vieja.
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    db.transfer(
        perfil="juan",
        moneda_origen="USD", cuenta_origen="Efectivo", monto_origen=40.0,
        moneda_destino="USD", cuenta_destino="Binance", monto_destino=40.0,
    )
    db.set_wallet_balance("juan", "Bs", "BDV", 500.0)  # snapshot suelto, no pareja
    assert db.peek_last_transfer("juan") is None


def test_undo_transfer_by_ids_revierte_ambas_billeteras(db):
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    db.transfer(
        perfil="juan",
        moneda_origen="USD", cuenta_origen="Efectivo", monto_origen=40.0,
        moneda_destino="USD", cuenta_destino="Binance", monto_destino=40.0,
    )
    pendiente = db.peek_last_transfer("juan")
    resultado = db.undo_transfer_by_ids(
        "juan", pendiente["snapshot_id_origen"], pendiente["snapshot_id_destino"]
    )
    assert resultado is not None
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 100.0
    assert db.get_wallet_balance("juan", "USD", "Binance") == 0.0
    # Los snapshots ya se borraron -- no debe quedar nada que deshacer de nuevo.
    assert db.peek_last_transfer("juan") is None


def test_undo_transfer_by_ids_con_ids_invalidos_retorna_none(db):
    assert db.undo_transfer_by_ids("juan", 9999, 9998) is None
