"""Pruebas del estado interno del bot (latido de vida) -- ver
src/storage/health.py."""


def test_estado_get_de_clave_inexistente_es_none(db):
    assert db.estado_get("latido") is None


def test_estado_set_y_get(db):
    db.estado_set("latido", "12345.6")
    assert db.estado_get("latido") == "12345.6"


def test_estado_set_sobreescribe_valor_anterior(db):
    db.estado_set("latido", "1")
    db.estado_set("latido", "2")
    assert db.estado_get("latido") == "2"


def test_estado_claves_independientes(db):
    db.estado_set("latido", "1")
    db.estado_set("salud_alertada", "1")
    assert db.estado_get("latido") == "1"
    assert db.estado_get("salud_alertada") == "1"
