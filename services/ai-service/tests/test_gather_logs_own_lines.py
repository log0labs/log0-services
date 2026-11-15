import pytest

from app.graph.nodes.gather_logs import message_query


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        (["payment gateway unavailable, retry 2/5", "other"], "payment gateway unavailable, retry"),
        (["panic serving POST /charge: assignment to entry in nil map"], "panic serving POST /charge: assignment to entry in nil map"),
        (["GET /cart/k6-1-837-1794696599997-837 failed: unknown sku sku-1004"], None),  # too little before the first id
        (["order 378d4b87 failed"], None),
        ([], None),
        (["x" * 200], "x" * 60),
    ],
)
def test_the_query_is_the_stable_start_of_the_message(messages, expected):
    assert message_query(messages) == expected
