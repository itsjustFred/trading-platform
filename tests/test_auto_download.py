from data_tools.auto_download import _auto_download_source


def test_index_cfd_aliases_use_dukascopy_source():
    assert _auto_download_source("SP500", "1m") == "dukascopy"
    assert _auto_download_source("NAS100", "15m") == "dukascopy"
    assert _auto_download_source("US100", "15m") == "dukascopy"


def test_us_stock_intraday_still_uses_intraday_stock_plan():
    assert _auto_download_source("AAPL", "1h") == "huggingface"
