from analysis.watchlist import ScoredHeadline, rank_watchlist
from schemas import Bias, CatalystType


def _h(ticker: str, bias: Bias, conf: float, catalyst: CatalystType = CatalystType.EARNINGS, title: str = "t") -> ScoredHeadline:
    return ScoredHeadline(ticker=ticker, bias=bias, catalyst=catalyst, confidence=conf, title=title)


def test_ranks_by_confidence_and_truncates_to_size():
    entries = rank_watchlist(
        [
            _h("AAA", Bias.BULLISH, 0.6),
            _h("BBB", Bias.BULLISH, 0.9),
            _h("CCC", Bias.BULLISH, 0.75),
        ],
        size=2,
        allow_shorts=False,
    )
    assert [e.ticker for e in entries] == ["BBB", "CCC"]
    assert [e.rank for e in entries] == [1, 2]


def test_neutral_and_non_material_items_are_excluded():
    entries = rank_watchlist(
        [
            _h("AAA", Bias.NEUTRAL, 0.9),
            _h("BBB", Bias.BULLISH, 0.9, catalyst=CatalystType.NONE),
            _h("CCC", Bias.BULLISH, 0.0),
        ],
        size=5,
        allow_shorts=True,
    )
    assert entries == []


def test_bearish_names_dropped_unless_shorts_allowed():
    headlines = [_h("DOWN", Bias.BEARISH, 0.8, CatalystType.GUIDANCE)]
    assert rank_watchlist(headlines, size=5, allow_shorts=False) == []
    shorts = rank_watchlist(headlines, size=5, allow_shorts=True)
    assert shorts[0].bias is Bias.BEARISH


def test_corroboration_raises_score_and_conflict_lowers_it():
    clean = rank_watchlist(
        [_h("X", Bias.BULLISH, 0.7), _h("X", Bias.BULLISH, 0.7)], size=1, allow_shorts=False
    )[0]
    conflicted = rank_watchlist(
        [_h("Y", Bias.BULLISH, 0.7), _h("Y", Bias.BEARISH, 0.7)], size=1, allow_shorts=True
    )[0]
    single = rank_watchlist([_h("Z", Bias.BULLISH, 0.7)], size=1, allow_shorts=False)[0]
    assert clean.target_confidence > single.target_confidence
    assert clean.supporting_headlines == 2
    # Conflict: dominant is BULLISH on a tie, so the score is penalised by the opposing share.
    assert conflicted.target_confidence < single.target_confidence


def test_score_is_bounded_to_unit_interval():
    entries = rank_watchlist(
        [_h("Q", Bias.BULLISH, 0.99) for _ in range(10)], size=1, allow_shorts=False
    )
    assert 0.0 <= entries[0].target_confidence <= 1.0
