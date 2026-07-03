"""Fixtures compartidas para los tests. Cada test recibe una DBClient nueva
en memoria (rápida, aislada, no toca gastos.db real)."""
import pytest
from src.storage.db import DBClient


@pytest.fixture
def db():
    client = DBClient(":memory:", initial_balance=1000.0, fixed_categories=["Supermercado", "Salidas"])
    yield client
    client.close()
