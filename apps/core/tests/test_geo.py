from apps.core.geo import parse_latlon

DEFAULT = (38.9531, -77.4565)


def test_parses_valid_pair():
    assert parse_latlon("40.7,-74.0", DEFAULT) == (40.7, -74.0)


def test_tolerates_whitespace():
    assert parse_latlon("  40.7 , -74.0 ", DEFAULT) == (40.7, -74.0)


def test_malformed_returns_default():
    for bad in ["", None, "abc", "40.7", "40.7,-74.0,9", "x,y"]:
        assert parse_latlon(bad, DEFAULT) == DEFAULT
