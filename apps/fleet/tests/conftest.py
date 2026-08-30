import pytest

from apps.accounts.factories import UserFactory


@pytest.fixture
def logged_in_client(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def owner_client(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client
