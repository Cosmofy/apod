from datetime import date
from unittest.mock import Mock

from app import database


def test_vector_search_computes_distance_for_index_candidates(
    monkeypatch,
) -> None:
    row = (
        "2024-01-01",
        "A Spiral Galaxy",
        "A galaxy with spiral arms.",
        "https://example.com/image.jpg",
        None,
        "image",
        None,
        None,
        0.125,
    )
    cursor = Mock()
    cursor.fetchall.return_value = [row]
    connection = Mock()
    connection.execute.return_value = cursor
    monkeypatch.setattr(database, "connect_database", lambda: connection)

    matches = database.search_vector_apods([0.0] * 3072, 5)

    statement, parameters = connection.execute.call_args.args
    assert "vector_top_k" in statement
    assert "vector_distance_cos" in statement
    assert parameters[0] == parameters[1]
    assert parameters[2] == 5
    assert matches[0].apod.date == date(2024, 1, 1)
    assert matches[0].metric == 0.125
    connection.close.assert_called_once()
