"""Pruebas de metas de ahorro -- ver SavingsMixin en src/storage/savings.py.
A propósito NO tocan ninguna billetera: son solo un contador de progreso
hacia un objetivo.
"""


def test_create_goal_empieza_en_cero(db):
    goal_id = db.create_goal("juan", "viaje", "USD", 500.0)
    assert goal_id > 0
    metas = db.list_goals("juan")
    assert len(metas) == 1
    assert metas[0]["nombre"] == "viaje"
    assert metas[0]["monto_actual"] == 0.0
    assert metas[0]["monto_objetivo"] == 500.0
    assert metas[0]["cumplida"] is False
    assert metas[0]["porcentaje"] == 0.0


def test_create_goal_no_toca_ninguna_billetera(db):
    db.set_wallet_balance("juan", "USD", "Efectivo", 100.0)
    db.create_goal("juan", "viaje", "USD", 500.0)
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 100.0


def test_contribute_goal_suma_progreso(db):
    db.create_goal("juan", "viaje", "USD", 500.0)
    resultado = db.contribute_goal("juan", "viaje", 100.0)
    assert resultado["encontrada"] is True
    assert resultado["monto_actual"] == 100.0
    assert resultado["cumplida"] is False
    metas = db.list_goals("juan")
    assert metas[0]["monto_actual"] == 100.0
    assert metas[0]["porcentaje"] == 20.0


def test_contribute_goal_es_insensible_a_mayusculas(db):
    db.create_goal("juan", "Viaje", "USD", 500.0)
    resultado = db.contribute_goal("juan", "viaje", 100.0)
    assert resultado["encontrada"] is True
    assert resultado["nombre"] == "Viaje"


def test_contribute_goal_meta_inexistente(db):
    resultado = db.contribute_goal("juan", "no existe", 100.0)
    assert resultado == {"encontrada": False}


def test_contribute_goal_alcanza_objetivo_se_marca_cumplida(db):
    db.create_goal("juan", "viaje", "USD", 100.0)
    resultado = db.contribute_goal("juan", "viaje", 100.0)
    assert resultado["cumplida"] is True
    # Ya cumplida: no debe salir en list_goals con solo_activas=True (default)
    assert db.list_goals("juan") == []
    todas = db.list_goals("juan", solo_activas=False)
    assert todas[0]["cumplida"] is True


def test_contribute_goal_supera_objetivo_no_se_recorta(db):
    db.create_goal("juan", "viaje", "USD", 100.0)
    resultado = db.contribute_goal("juan", "viaje", 150.0)
    assert resultado["monto_actual"] == 150.0
    assert resultado["cumplida"] is True


def test_contribute_goal_varios_aportes_acumulan(db):
    db.create_goal("juan", "viaje", "USD", 500.0)
    db.contribute_goal("juan", "viaje", 100.0)
    resultado = db.contribute_goal("juan", "viaje", 150.0)
    assert resultado["monto_actual"] == 250.0


def test_contribute_goal_no_toca_ninguna_billetera(db):
    db.set_wallet_balance("juan", "USD", "Efectivo", 300.0)
    db.create_goal("juan", "viaje", "USD", 500.0)
    db.contribute_goal("juan", "viaje", 100.0)
    assert db.get_wallet_balance("juan", "USD", "Efectivo") == 300.0


def test_metas_aisladas_por_perfil(db):
    db.create_goal("juan", "viaje", "USD", 500.0)
    db.create_goal("maria", "viaje", "USD", 999.0)
    assert len(db.list_goals("juan")) == 1
    assert db.list_goals("juan")[0]["monto_objetivo"] == 500.0
    assert db.list_goals("maria")[0]["monto_objetivo"] == 999.0


def test_list_goals_solo_activas_por_defecto(db):
    db.create_goal("juan", "viaje", "USD", 100.0)
    db.create_goal("juan", "celular", "USD", 200.0)
    db.contribute_goal("juan", "viaje", 100.0)  # se cumple
    activas = db.list_goals("juan")
    assert len(activas) == 1
    assert activas[0]["nombre"] == "celular"
    todas = db.list_goals("juan", solo_activas=False)
    assert len(todas) == 2
