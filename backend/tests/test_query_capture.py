import pytest

from multisearch.query_capture import retainable_query


def test_retainable_query_preserves_the_exact_value():
    query = "方法 🧪\nAND 'result' \\ reference"
    assert retainable_query(query) == query
    assert retainable_query("字" * 1024) == "字" * 1024


@pytest.mark.parametrize("query", [None, 1, "", "x" * 1025, "nul\x00", "unpaired\ud800"])
def test_unsupported_retention_text_is_skipped(query):
    assert retainable_query(query) is None
