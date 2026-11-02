import pytest

from enrich import evaluate
from enrich.calibrate import calibrate, fit, load_pairs, pearson, ranks, save_pairs, sign
from enrich.evaluate import score_mood
from test_enrich import DOMAINS, FakeModel
from test_mood_marker import con  # noqa: F401 - the fixture with rated entries


def test_fit_and_correlation():
    assert fit([1, 2, 3], [2, 4, 6]) == pytest.approx((0.0, 2.0))
    assert fit([5, 5, 5], [1, 2, 3]) == (2.0, 0.0)                         # a constant model: no slope
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0) and pearson([1, 1, 1], [1, 2, 3]) is None
    assert ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]                 # ties share their mean rank
    assert (sign(0.4, 0.5), sign(0.6, 0.5), sign(-0.6, 0.5), sign(0)) == (0, 1, -1, 0)


def test_a_narrow_scale_is_found_and_the_stretch_helps():
    ratings = [-4, -3, -2, -1, 0, 1, 2, 3, 4, -4, -2, 2, 4, 3, -3]
    pairs = [(float(r), r / 2) for r in ratings]                           # the model only uses half the range
    result = calibrate(pairs)
    assert (result.a, result.b) == pytest.approx((0.0, 2.0)) and result.pearson == pytest.approx(1.0)
    assert result.raw_error == pytest.approx(sum(abs(r) / 2 for r in ratings) / len(ratings))
    assert result.loo_error == pytest.approx(0.0, abs=1e-9)                # leave-one-out: still exact
    assert result.loo_same_sign == 1.0
    text = str(result)
    assert "scale is too narrow" in text and "Pearson 1.00" in text and "leave-one-out" in text


def test_noise_is_not_turned_into_a_gain():
    pairs = [(float(r), float(m)) for r, m in zip([-3, 2, -1, 4, 0, 1, -2, 3, 2, -4, 1, 0], [1, -1, 2, 0, -2, 1, 0, -1, 2, 1, -1, 0])]
    result = calibrate(pairs)
    assert abs(result.pearson) < 0.4 and result.loo_error > 1.0
    with pytest.raises(ValueError, match="at least"):
        calibrate(pairs[:5])


def test_pairs_are_saved_without_text_and_the_command_reads_them(con, tmp_path, capsys):  # noqa: F811
    pairs: list = []
    score_mood(con, FakeModel(), DOMAINS, pairs=pairs)
    assert [p[1:3] for p in pairs] == [(4, 4), (-2, 0)]                    # (rating, model value); one entry skipped
    path = tmp_path / "mood_pairs.json"
    save_pairs(path, "fake", "6:x", pairs)
    assert "Radtour" not in path.read_text(encoding="utf-8")               # numbers only
    info, loaded = load_pairs(path)
    assert info["model"] == "fake" and loaded == [(4.0, 4.0), (-2.0, 0.0)]
    with pytest.raises(SystemExit, match="at least"):
        evaluate.main(["calibrate", "--pairs", str(path)])                 # too few pairs for a fit
    save_pairs(path, "fake", "6:x", [(i, i, r % 5, 100) for i, r in enumerate(range(20))])
    evaluate.main(["calibrate", "--pairs", str(path)])
    out = capsys.readouterr().out
    assert "Pairs from the run with fake, extraction version 6:x" in out and "pairs: 20" in out
    with pytest.raises(SystemExit, match="not found"):
        evaluate.main(["calibrate", "--pairs", str(tmp_path / "missing.json")])
