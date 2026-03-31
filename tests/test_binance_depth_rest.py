"""Unit tests for REST depth fetch (no network)."""
from unittest.mock import MagicMock, patch

from data_feed.binance_depth_rest import (
    BinanceDepthBook,
    depth_book_to_engine_update,
    fetch_binance_depth_book,
)


def test_fetch_binance_depth_book_parses_json():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "lastUpdateId": 42,
        "bids": [["100.5", "1.0"], ["100.0", "2"]],
        "asks": [["101.0", "3"]],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("data_feed.binance_depth_rest.requests.get", return_value=mock_resp):
        book = fetch_binance_depth_book(
            "https://fapi.binance.com/fapi/v1/depth",
            "BTCUSDT",
            limit=1000,
            timeout=5.0,
        )

    assert book.last_update_id == 42
    assert book.bids == [(100.5, 1.0), (100.0, 2.0)]
    assert book.asks == [(101.0, 3.0)]
    mock_resp.raise_for_status.assert_called_once()


def test_depth_book_to_engine_update_builds_lists():
    ofe = MagicMock()
    ofe.DepthUpdate.return_value = MagicMock()
    ofe.DepthLevel.side_effect = lambda: MagicMock()

    book = BinanceDepthBook(
        last_update_id=7,
        bids=[(10.0, 1.0)],
        asks=[(11.0, 2.0)],
    )
    depth_book_to_engine_update(book, ofe, timestamp_ms=12345)

    snap = ofe.DepthUpdate.return_value
    assert snap.timestamp == 12345
    assert snap.first_update_id == 7
    assert snap.final_update_id == 7
    assert snap.is_snapshot is True
    assert len(snap.bids) == 1
    assert len(snap.asks) == 1
