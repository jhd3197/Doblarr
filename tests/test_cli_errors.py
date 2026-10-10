"""A mistake on the command line gets one readable line, not a traceback."""

import pytest

from doblarr.cli import main


def test_an_unknown_series_is_one_line(capsys):
    assert main(["lookup", "series-that-does-not-exist"]) == 1
    err = capsys.readouterr().err
    assert "doblarr lookup: unknown series series-that-does-not-exist" in err
    assert "Traceback" not in err


def test_verbose_keeps_the_traceback():
    with pytest.raises(KeyError):
        main(["-v", "lookup", "series-that-does-not-exist"])
