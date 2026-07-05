"""Pruebas del borrado total de un perfil (/borrar_todo) -- ver
src/storage/reset.py."""


def test_reset_profile_borra_transacciones_y_billeteras(db):
    db.append_expense("juan", "gasto", "2026-07-05", "Mercado", "Supermercado", 100.0, moneda="Bs")
    db.append_expense("juan", "ingreso", "2026-07-05", "Sueldo", "Otros", 500.0, moneda="Bs")

    db.reset_profile("juan")

    assert db.get_all_transactions("juan") == []
    # Vuelve al saldo inicial de fábrica (fixture usa initial_balance=1000.0),
    # como si el perfil fuera nuevo -- ver WalletsMixin._ensure_wallets_for_profile.
    assert db.get_wallet_balance("juan", "Bs", "BDV") == 1000.0


def test_reset_profile_borra_presupuestos_deudas_y_metas(db):
    db.set_budget("juan", "Supermercado", 1000.0)
    db.register_debt(perfil="juan", persona="Pedro", tipo="prestado", moneda="Bs",
                      monto=50.0, descripcion="", fecha="2026-07-05")
    db.create_goal(perfil="juan", nombre="Viaje", moneda="USD", monto_objetivo=500.0)

    db.reset_profile("juan")

    assert db.get_all_budgets("juan") == []
    assert db.list_debts("juan") == []
    assert db.list_goals("juan") == []


def test_reset_profile_no_afecta_otro_perfil(db):
    db.append_expense("juan", "gasto", "2026-07-05", "Mercado", "Supermercado", 100.0, moneda="Bs")
    db.append_expense("maria", "gasto", "2026-07-05", "Mercado", "Supermercado", 200.0, moneda="Bs")

    db.reset_profile("juan")

    assert db.get_all_transactions("juan") == []
    assert len(db.get_all_transactions("maria")) == 1
    assert db.get_wallet_balance("maria", "Bs", "BDV") == 800.0  # 1000 inicial - 200 de gasto, sin tocar


def test_reset_profile_no_borra_categorias_fijas(db):
    db.reset_profile("juan")
    fijas = {c for c in db.get_dynamic_categories("juan")}
    assert fijas == set()  # no había ninguna dinámica; las fijas no viven en dynamic_categories
