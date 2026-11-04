from timemachine.evaluate import summarize


def test_empty_points_are_not_scored_as_zero():
    # only points that had positives are appended by replay(); summarize averages exactly those
    rows = {"risky": [(0.5, 0.5, 0.5, 10, 2)], "tests": [], "dups": []}
    out = summarize(rows, [], "x")
    assert out["output1_risky_change"]["f1"] == 0.5 and out["output1_risky_change"]["replay_points"] == 1
    assert out["output2_missing_tests"]["f1"] is None
