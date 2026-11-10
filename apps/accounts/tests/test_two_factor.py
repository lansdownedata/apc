import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts import two_factor
from apps.accounts.factories import SignInCodeFactory, UserFactory
from apps.accounts.models import SignInCode
from apps.accounts.two_factor import ResendLimited, Subject, Verdict
from apps.contacts.factories import ContactFactory
from apps.planners.models import PlannerAccount

pytestmark = pytest.mark.django_db

NOW = "django.utils.timezone.now"


@pytest.fixture
def subject() -> Subject:
    user = UserFactory(phone="+12025550123", first_name="Sam")
    return two_factor.staff_subject(user)


def at(delta: timedelta, base):
    return patch(NOW, return_value=base + delta)


def test_staff_subject_copies_the_user_fields():
    user = UserFactory(phone="+12025550123", first_name="Sam")
    s = two_factor.staff_subject(user)
    assert (s.kind, s.pk, s.phone, s.first_name, s.email, s.password_hash) == (
        "staff",
        user.pk,
        "+12025550123",
        "Sam",
        user.email,
        user.password,
    )


def test_issue_returns_six_digits_and_stores_only_a_hash(subject):
    row, raw = two_factor.issue(subject, "sms")
    assert re.fullmatch(r"\d{6}", raw) and raw not in row.code_hash
    assert row.expires_at - row.created_at == timedelta(minutes=10)
    assert row.user_id == subject.pk and row.planner_id is None


def test_verify_ok_consumes_the_code(subject):
    _, raw = two_factor.issue(subject, "sms")
    assert two_factor.verify(subject, raw) is Verdict.OK
    assert two_factor.verify(subject, raw) is Verdict.MISSING


def test_verify_ignores_spaces_and_dashes(subject):
    _, raw = two_factor.issue(subject, "sms")
    assert two_factor.verify(subject, f" {raw[:3]}-{raw[3:]} ") is Verdict.OK


def test_expired_after_ten_minutes(subject):
    row, raw = two_factor.issue(subject, "sms")
    with at(timedelta(minutes=10, seconds=1), row.created_at):
        assert two_factor.verify(subject, raw) is Verdict.EXPIRED


def test_fifth_wrong_try_locks_and_voids(subject):
    _, raw = two_factor.issue(subject, "sms")
    wrong = "000000" if raw != "000000" else "111111"
    assert [two_factor.verify(subject, wrong) for _ in range(4)] == [Verdict.WRONG] * 4
    assert two_factor.verify(subject, wrong) is Verdict.LOCKED
    assert two_factor.verify(subject, raw) is Verdict.LOCKED


def test_new_code_voids_the_earlier_one(subject):
    first, old = two_factor.issue(subject, "sms")
    with at(timedelta(seconds=61), first.created_at):
        _, new = two_factor.issue(subject, "sms")
        if old != new:
            assert two_factor.verify(subject, old) is Verdict.WRONG
        assert two_factor.verify(subject, new) is Verdict.OK
    first.refresh_from_db()
    assert first.consumed_at is not None


def test_resend_inside_60_seconds_is_limited(subject):
    row, _ = two_factor.issue(subject, "sms")
    with at(timedelta(seconds=20), row.created_at), pytest.raises(ResendLimited) as err:
        two_factor.issue(subject, "sms")
    assert 0 < err.value.retry_in <= 60


def test_sixth_issue_in_an_hour_is_limited(subject):
    first, _ = two_factor.issue(subject, "sms")
    for i in range(1, 5):
        with at(timedelta(seconds=61 * i), first.created_at):
            two_factor.issue(subject, "sms")
    with at(timedelta(seconds=61 * 5), first.created_at), pytest.raises(ResendLimited) as err:
        two_factor.issue(subject, "sms")
    assert 60 < err.value.retry_in <= 3600


def test_staff_code_does_not_verify_a_planner(subject):
    _, raw = two_factor.issue(subject, "sms")
    planner = Subject("planner", subject.pk, "", "p@example.com", "P", "hash")
    assert two_factor.verify(planner, raw) is Verdict.MISSING


def test_issue_deletes_rows_older_than_24_hours(subject):
    old = SignInCodeFactory(user_id=subject.pk)
    SignInCode.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
    two_factor.issue(subject, "sms")
    assert not SignInCode.objects.filter(pk=old.pk).exists()


def test_exactly_one_owner_constraint():
    expires = timezone.now() + timedelta(minutes=10)
    with pytest.raises(IntegrityError), transaction.atomic():
        SignInCode.objects.create(code_hash="x", channel="sms", expires_at=expires)
    user = UserFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        SignInCode.objects.create(
            user=user,
            planner=PlannerAccount.objects.create(contact=ContactFactory(), email="p@example.com"),
            code_hash="x",
            channel="sms",
            expires_at=expires,
        )


def test_enabled_reads_the_setting(settings):
    settings.TWO_FACTOR_ENABLED = False
    assert two_factor.enabled() is False
    settings.TWO_FACTOR_ENABLED = True
    assert two_factor.enabled() is True
