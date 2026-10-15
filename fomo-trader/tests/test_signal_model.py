import json
import time

import pytest

from analysis import signal_model


def test_standardize_and_logistic_separation():
    rows = [[-3.], [-2.], [-1.], [1.], [2.], [3.]]
    transformed, means, stds = signal_model.standardize(rows)
    assert means == [0]
    assert stds[0] > 0
    weights, bias = signal_model.fit(transformed, [0, 0, 0, 1, 1, 1], l2=.1)
    assert signal_model.sigmoid(weights[0]*transformed[0][0]+bias) < .5
    assert signal_model.sigmoid(weights[0]*transformed[-1][0]+bias) > .5


def test_threshold_and_fast_inference():
    model = signal_model.SignalModel({"feature_names": ["x"], "weights": [2],
                                      "means": [0], "stds": [1], "bias": 0, "threshold": .6})
    assert model.score({"x": 0}) == .5
    assert not model.approve({"x": 0})
    assert model.approve({"x": 1})
    start = time.perf_counter()
    for _ in range(1000):
        model.score({"x": 1})
    assert (time.perf_counter() - start) / 1000 < .05


@pytest.mark.parametrize("content", [None, "{broken", '{}'])
def test_fail_open(content, tmp_path):
    path = tmp_path / "model.json"
    if content is not None:
        path.write_text(content)
    logs = []
    assert signal_model.approve({"x": 1}, path=path, log=logs.append)
    assert len(logs) == 1 and logs[0].startswith("MODEL FALLBACK")


def test_loaded_model(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"feature_names": ["x"], "weights": [2],
                                "means": [0], "stds": [1], "bias": 0, "threshold": .6}))
    model = signal_model.SignalModel(signal_model.load_model(path))
    assert model.approve({"x": 1})
