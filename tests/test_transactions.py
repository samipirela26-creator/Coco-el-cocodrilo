"""Pruebas de la aritmética de gasto/ingreso y su reversión (/deshacer).
Estas son las funciones más delicadas del bot: si algo aquí se rompe,
el saldo de alguien queda mal en producción sin que nadie lo note al toque.
"""
import pytest


def test_gasto_resta_del_saldo(db):
    saldo_inicial = db.get_wallet_balance("juan", "Bs", "BDV")
    db.append_expense(perfil="juan", tipo="gasto", fecha="2026-07-01",
                       descripcion="Mercado", categoria="Supermercado", monto=150.0,
                       moneda="Bs")
    assert db.get_wallet_balance("juan", "Bs", "BDV") == saldo_inicial - 150.0


def test_ingreso_suma_al_saldo(db):
    saldo_inicial = db.get_wallet_balance("juan", "USD", "Efectivo")
    db.append_expense(perfil="juan", tipo="ingreso", fecha="2026-07-01",
                       descripcion="Pago", categoria="Salario", monto=200.0,
                       moneda="USD", cuenta="Efectivo")
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == saldo_inicial + 200.0


def test_deshacer_revierte_el_gasto_exacto(db):
    saldo_inicial = db.get_wallet_balance("juan", "Bs", "BDV")
    db.append_expense(perfil="juan", tipo="gasto", fecha="2026-07-01",
                       descripcion="Mercado", categoria="Supermercado", monto=150.0,
                       moneda="Bs")
    deshecho = db.delete_last_transaction("juan")
    assert deshecho["monto"] == 150.0
    assert db.get_wallet_balance("juan", "Bs", "BDV") == saldo_inicial


def test_deshacer_sin_movimientos_retorna_none(db):
    assert db.delete_last_transaction("perfil_vacio") is None


def test_deshacer_solo_afecta_el_ultimo_movimiento(db):
    db.append_expense(perfil="juan", tipo="gasto", fecha="2026-07-01",
                       descripcion="Primero", categoria="Supermercado", monto=50.0, moneda="Bs")
    db.append_expense(perfil="juan", tipo="gasto", fecha="2026-07-02",
                       descripcion="Segundo", categoria="Salidas", monto=30.0, moneda="Bs")
    deshecho = db.delete_last_transaction("juan")
    assert deshecho["descripcion"] == "Segundo"
    # El primer gasto (50 Bs) debe seguir descontado -- solo se revierte el último.
    assert db.get_wallet_balance("juan", "Bs", "BDV") == 1000.0 - 50.0


def test_perfiles_estan_aislados(db):
    db.append_expense(perfil="juan", tipo="gasto", fecha="2026-07-01",
                       descripcion="Solo de juan", categoria="Supermercado", monto=100.0, moneda="Bs")
    # El saldo inicial de "maria" no debe verse afectado por lo que hace "juan".
    assert db.get_wallet_balance("maria", "Bs", "BDV") == 1000.0
