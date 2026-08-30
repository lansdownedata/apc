import pytest
from django.db import connection

from apps.accounts.factories import UserFactory


@pytest.fixture(autouse=True)
def reset_fleet_driver_autoincrement():
    """Reset the fleet_driver table's AUTO_INCREMENT before each test to ensure
    driver_number allocation is deterministic (1000 for the first driver in each test)."""
    yield
    # After each test, truncate the table and reset AUTO_INCREMENT
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE TABLE fleet_driver")
        cursor.execute("ALTER TABLE fleet_driver AUTO_INCREMENT = 1")


@pytest.fixture
def logged_in_client(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def owner_client(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client
