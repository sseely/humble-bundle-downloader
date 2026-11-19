import humble_dl

EXPECTED_VERSION = "0.1.0"


def test_version_is_declared() -> None:
    assert humble_dl.__version__ == EXPECTED_VERSION
