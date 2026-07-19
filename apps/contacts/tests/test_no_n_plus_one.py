"""Guards the contacts directory against a phone-related N+1.

`Contact.primary_phone` used to call `.filter(is_primary=True)` on the `phones`
related manager, which bypasses `prefetch_related` and re-queries per row.
"""

import pytest

from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


def _make_contacts(n):
    for _ in range(n):
        contact = ContactFactory()
        contact.add_phone("(305) 555-0199", label="work")


def test_directory_does_not_issue_a_phone_query_per_row(
    logged_in_client, django_assert_num_queries
):
    _make_contacts(3)
    # Warm up (URL resolution, sessions, etc. shouldn't count toward the assertion).
    logged_in_client.get("/contacts/")
    with django_assert_num_queries(7):
        baseline = logged_in_client.get("/contacts/")
    assert baseline.status_code == 200


def test_query_count_is_constant_regardless_of_row_count(
    logged_in_client, django_assert_num_queries
):
    """The crux of the fix: 8 contacts must cost the same query count as 3 — not
    scale with the number of rows (that would mean the phone lookup is per-row)."""
    _make_contacts(8)
    logged_in_client.get("/contacts/")
    with django_assert_num_queries(7):
        resp = logged_in_client.get("/contacts/")
    assert resp.status_code == 200
