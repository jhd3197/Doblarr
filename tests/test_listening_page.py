"""The standalone listening page keeps subtitle text as data."""

import json

from doblarr.listening_page import render


def test_text_that_closes_a_script_tag_stays_data(tmp_path):
    note = 'Kaito: "</script><script>alert(1)</script>" & __CSS__'
    html = render({"comparison_id": "c1", "note": note, "labels": {}, "objective": {},
                   "scenes": []}, tmp_path)
    assert "<script>alert(1)" not in html
    embedded = html.split("const DATA = ", 1)[1].split(";</script>", 1)[0]
    assert json.loads(embedded)["note"] == note
