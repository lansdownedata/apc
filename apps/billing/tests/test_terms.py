"""APC-96 — one resolver for a stored term: label, QuickBooks id and due date."""

from datetime import date
from unittest.mock import patch

import pytest

from apps.billing import qbo_sync, services, terms
from apps.billing.models import QboTerm, Terms
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db
REALM = "123"


@pytest.fixture(autouse=True)
def realm():
    with patch("apps.billing.qbo_sync.current_realm", return_value=REALM):
        yield


def _term(qbo_id="3", name="Net 30", **kw):
    return QboTerm.objects.create(realm_id=REALM, qbo_id=qbo_id, name=name, **kw)


def test_resolve_legacy_code_without_quickbooks():
    got = terms.resolve("net_30")
    assert (got.label, got.qbo_id) == ("Net 30", None)
    assert got.rule == terms.TermRule("STANDARD", days=30)
    assert terms.due("net_30", date(2026, 1, 20)) == date(2026, 2, 19)
    assert terms.resolve("") == terms.ResolvedTerm("", "", None, None)


def test_resolve_legacy_code_takes_quickbooks_days_when_matched():
    _term(term_type="STANDARD", due_days=31)
    got = terms.resolve("net_30")
    assert got.qbo_id == "3"
    assert terms.due("net_30", date(2026, 1, 20)) == date(2026, 2, 20)


def test_resolve_quickbooks_name():
    _term("9", "Net 60", term_type="STANDARD", due_days=60)
    got = terms.resolve("Net 60")
    assert (got.label, got.qbo_id) == ("Net 60", "9")
    assert terms.due("Net 60", date(2026, 1, 1)) == date(2026, 3, 2)
    assert terms.is_quickbooks_name("Net 60") and not terms.is_quickbooks_name("net_30")


def test_renamed_quickbooks_term_is_unmatched_not_guessed():
    _term(name="Net 30")
    services.create_account(
        ContactFactory(), name="Ridgeline", group_name="AP", terms="Net 30 days"
    )
    got = terms.resolve("Net 30 days")
    assert (got.label, got.qbo_id) == ("Net 30 days", None)
    assert got.rule == terms.TermRule("STANDARD", days=30)
    assert qbo_sync.term_ref("Net 30 days") is None
    assert "Net 30 days" in qbo_sync.unmatched_terms()


def test_date_driven_due_rule():
    _term("5", "Mid month", term_type="DATE_DRIVEN", day_of_month_due=15, due_next_month_days=10)
    assert terms.due("Mid month", date(2026, 11, 3)) == date(2026, 11, 15)
    assert terms.due("Mid month", date(2026, 11, 12)) == date(2026, 12, 15)


def test_date_driven_due_clamps_to_month_end():
    _term("5", "End", term_type="DATE_DRIVEN", day_of_month_due=31)
    assert terms.due("End", date(2027, 2, 1)) == date(2027, 2, 28)


def test_choices_fallback_and_connected():
    assert terms.choices() == list(Terms.choices)
    _term("1", "Zed")
    _term("2", "Alpha")
    with patch("apps.billing.terms.qbo.is_connected", return_value=False):
        assert terms.choices() == list(Terms.choices)
    with patch("apps.billing.terms.qbo.is_connected", return_value=True):
        assert terms.choices() == [("Alpha", "Alpha"), ("Zed", "Zed")]
        got = terms.choices("Gone")
        assert got[-1] == ("Gone", "Gone") and got.count(("Gone", "Gone")) == 1
        assert terms.choices("Zed") == [("Alpha", "Alpha"), ("Zed", "Zed")]


def test_unmatched_terms_lists_names_and_codes():
    _term(name="Net 15")
    services.create_account(ContactFactory(), name="A", group_name="G", terms="net_30")
    services.create_account(ContactFactory(), name="B", group_name="G", terms="net_15")
    services.create_account(ContactFactory(), name="C", group_name="G", terms="Odd")
    assert qbo_sync.unmatched_terms() == ["Net 30", "Odd"]


def test_term_ref_uses_resolver():
    _term("3", "Net 30")
    assert qbo_sync.term_ref("net_30") == {"value": "3"}
    assert qbo_sync.term_ref("Net 30") == {"value": "3"}
    assert qbo_sync.term_ref("") is None


def test_choices_keeps_the_stored_code_over_a_same_named_quickbooks_term():
    _term("3", "Net 30")
    _term("4", "Net 45")
    with patch("apps.billing.terms.qbo.is_connected", return_value=True):
        got = terms.choices("net_30")
    assert got == [("Net 45", "Net 45"), ("net_30", "Net 30")]
    assert [label for _, label in got].count("Net 30") == 1
    assert terms.resolve("net_30").value == "net_30"


def test_labels_match_resolve_in_one_batch(django_assert_max_num_queries):
    _term("3", "Net 30")
    _term("4", "Net 45")
    values = ["net_30", "net_15", "Net 45", "net 45", "Gone", ""]
    with django_assert_max_num_queries(2):  # the realm, then the company's term names
        got = terms.labels(values)
    assert got == {value: terms.resolve(value).label for value in values}
    assert got["net 45"] == "Net 45"
    assert terms.labels([]) == {}
