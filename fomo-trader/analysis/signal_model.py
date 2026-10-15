"""Pure-stdlib offline signal scoring. No entry-path integration by default."""

import json
import math
from pathlib import Path


def sigmoid(value):
    value = max(-40.0, min(40.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def standardize(rows):
    if not rows:
        raise ValueError("empty training set")
    width = len(rows[0])
    means = [sum(row[j] for row in rows) / len(rows) for j in range(width)]
    stds = [max((sum((row[j] - means[j]) ** 2 for row in rows) / len(rows)) ** .5, 1e-9)
            for j in range(width)]
    return [[(row[j] - means[j]) / stds[j] for j in range(width)] for row in rows], means, stds


def fit(rows, labels, l2=10.0, steps=3000, learning_rate=.05):
    """Batch gradient descent with average log loss and L2 penalty."""
    if len(rows) != len(labels) or not rows:
        raise ValueError("rows/labels mismatch")
    width = len(rows[0])
    weights, bias = [0.0] * width, 0.0
    n = len(rows)
    for _ in range(steps):
        errors = [sigmoid(sum(w*x for w, x in zip(weights, row)) + bias) - y
                  for row, y in zip(rows, labels)]
        gradient = [sum(e*row[j] for e, row in zip(errors, rows))/n + l2*weights[j]/n
                    for j in range(width)]
        weights = [w - learning_rate*g for w, g in zip(weights, gradient)]
        bias -= learning_rate * sum(errors) / n
    return weights, bias


def load_model(path):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    names = obj["feature_names"]
    if not names or any(len(obj[k]) != len(names) for k in ("weights", "means", "stds")):
        raise ValueError("invalid model dimensions")
    if any(not math.isfinite(float(v)) for k in ("weights", "means", "stds") for v in obj[k]):
        raise ValueError("nonfinite model parameter")
    if any(float(s) <= 0 for s in obj["stds"]):
        raise ValueError("invalid standard deviation")
    return obj


class SignalModel:
    def __init__(self, model):
        self.model = model

    def score(self, features):
        model = self.model
        z = float(model["bias"])
        for name, weight, mean, std in zip(model["feature_names"], model["weights"], model["means"], model["stds"]):
            z += float(weight) * (float(features[name]) - float(mean)) / float(std)
        return sigmoid(z)

    def approve(self, signal, threshold=None):
        return self.score(signal) >= float(self.model["threshold"] if threshold is None else threshold)


def approve(signal, threshold=None, path=None, log=print):
    """Fail open on any model or feature error, with one fallback log line."""
    try:
        model = SignalModel(load_model(path))
        return model.approve(signal, threshold)
    except Exception as exc:
        log("MODEL FALLBACK: %s" % type(exc).__name__)
        return True
