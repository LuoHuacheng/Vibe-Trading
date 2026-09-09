from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd


SKILL_PATH = Path(__file__).parents[1] / "src" / "skills" / "ml-strategy" / "SKILL.md"


def _python_block() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    return text.split("```python", 1)[1].split("```", 1)[0]


def _load_node(name: str, node_type: type[ast.AST], namespace: dict) -> object:
    tree = ast.parse(_python_block())
    node = next(
        item for item in tree.body if isinstance(item, node_type) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(SKILL_PATH), "exec"), namespace)
    return namespace[name]


class _IdentityScaler:
    def fit_transform(self, values):
        return values

    def transform(self, values):
        return values


class _RecordingModel:
    fits: list[np.ndarray] = []

    def __init__(self, *args, **kwargs):
        pass

    def fit(self, values, labels):
        self.fits.append(values.copy())
        return self

    def predict_proba(self, values):
        return np.array([[0.5, 0.5]])


def _namespace() -> dict:
    return {
        "np": np,
        "pd": pd,
        "StandardScaler": _IdentityScaler,
        "RandomForestClassifier": _RecordingModel,
        "GradientBoostingClassifier": _RecordingModel,
        "LogisticRegression": _RecordingModel,
    }


def test_walk_forward_purges_labels_not_observable_at_prediction_time() -> None:
    walk_forward_predict = _load_node(
        "walk_forward_predict",
        ast.FunctionDef,
        _namespace(),
    )
    features = pd.DataFrame({"row": np.arange(70, dtype=float)})
    labels = pd.Series(np.zeros(70, dtype=float))
    _RecordingModel.fits.clear()

    walk_forward_predict(features, labels, min_train_size=60, retrain_freq=100)

    assert _RecordingModel.fits[0][-1, 0] == 55.0


def test_one_bar_horizon_preserves_existing_training_window() -> None:
    walk_forward_predict = _load_node(
        "walk_forward_predict",
        ast.FunctionDef,
        _namespace(),
    )
    features = pd.DataFrame({"row": np.arange(70, dtype=float)})
    labels = pd.Series(np.zeros(70, dtype=float))
    _RecordingModel.fits.clear()

    walk_forward_predict(
        features,
        labels,
        min_train_size=60,
        retrain_freq=100,
        prediction_horizon=1,
    )

    assert _RecordingModel.fits[0][-1, 0] == 59.0


def test_signal_engine_preserves_unavailable_future_labels_as_nan() -> None:
    captured: dict[str, object] = {}

    def fake_predict(features, labels, **kwargs):
        captured["labels"] = labels.copy()
        captured["prediction_horizon"] = kwargs.get("prediction_horizon")
        return pd.Series(0.0, index=features.index)

    namespace = {
        "np": np,
        "pd": pd,
        "validate_data": lambda df: True,
        "build_features": lambda df: pd.DataFrame(
            {"x": np.arange(len(df))}, index=df.index
        ),
        "walk_forward_predict": fake_predict,
    }
    signal_engine = _load_node("SignalEngine", ast.ClassDef, namespace)

    frame = pd.DataFrame({"close": np.arange(1, 11, dtype=float)})
    signal_engine().generate({"TEST": frame})

    labels = captured["labels"]
    assert captured["prediction_horizon"] == 5
    assert labels.iloc[:5].eq(1.0).all()
    assert labels.iloc[-5:].isna().all()
