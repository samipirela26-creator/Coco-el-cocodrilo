"""Pruebas del bloqueo manual de usuarios (/bloquear, /desbloquear,
/bloqueados) -- ver src/storage/access_control.py."""


def test_usuario_nuevo_no_esta_bloqueado(db):
    assert db.is_blocked(12345) is False


def test_bloquear_y_desbloquear(db):
    db.block_user(12345, "Alguien", blocked_by=1)
    assert db.is_blocked(12345) is True
    assert db.unblock_user(12345) is True
    assert db.is_blocked(12345) is False


def test_desbloquear_a_alguien_no_bloqueado_retorna_false(db):
    assert db.unblock_user(99999) is False


def test_track_user_guarda_el_nombre_mas_reciente(db):
    db.track_user(111, "Juan", "juan")
    db.track_user(111, "Juan Perez", "juan")  # cambió de nombre en Telegram
    conocido = db.get_known_user(111)
    assert conocido["nombre"] == "Juan Perez"


def test_list_blocked_incluye_a_todos_los_bloqueados(db):
    db.block_user(1, "Uno", blocked_by=999)
    db.block_user(2, "Dos", blocked_by=999)
    bloqueados = {b["user_id"] for b in db.list_blocked()}
    assert bloqueados == {1, 2}
