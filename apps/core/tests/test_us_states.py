from apps.core.us_states import US_STATES


def test_has_50_states_plus_dc():
    assert len(US_STATES) == 51


def test_codes_are_two_letter_uppercase_and_unique():
    codes = [c for c, _ in US_STATES]
    assert all(len(c) == 2 and c.isupper() for c in codes)
    assert len(set(codes)) == 51


def test_known_pairs_present():
    d = dict(US_STATES)
    assert d["VA"] == "Virginia"
    assert d["DC"] == "District of Columbia"
    assert d["CA"] == "California"
