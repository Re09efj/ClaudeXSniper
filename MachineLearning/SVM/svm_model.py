"""
svm_model.py
NUMA 配置戦略の自動選択モデル - SVM 版 (RBF kernel, StandardScaler)

使い方:
    python svm_model.py --threads 4
    python svm_model.py --threads all --label energy_j
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "RandomForest"))
import random_forest as _rf
_rf.OUTPUTS_DIR = Path(__file__).resolve().parent.parent.parent / "Outputs" / "sizeS"
from random_forest import (
    collect_dataset, _build_allth_data,
    STRATEGIES, FEATURE_COLS, FEATURE_COLS_ALLTH, THREAD_NUMS,
    plot_confusion_matrix, plot_strategy_dist, _save,
)
OUTPUTS_DIR = _rf.OUTPUTS_DIR

MODEL_DIR = Path(__file__).parent
ML_DIR    = MODEL_DIR.parent


def make_pipeline():
    return Pipeline([
        ("scaler", StandardScaler()),
        ("svm",    SVC(kernel="rbf", C=10.0, gamma="scale",
                       class_weight="balanced", random_state=42,
                       decision_function_shape="ovr")),
    ])


def train_and_evaluate(X: pd.DataFrame, y: pd.Series) -> dict:
    loo = LeaveOneOut()
    y_true, y_pred = [], []

    for train_idx, test_idx in loo.split(X):
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue

        pipe = make_pipeline()
        pipe.fit(X.iloc[train_idx], y_train)
        y_pred.append(pipe.predict(X.iloc[test_idx])[0])

    # 最終モデルで permutation importance
    pipe_full = make_pipeline()
    pipe_full.fit(X, y)
    from sklearn.inspection import permutation_importance as pi
    r = pi(pipe_full, X, y, n_repeats=10, random_state=42)
    imp = pd.Series(r.importances_mean, index=FEATURE_COLS if X.shape[1] == len(FEATURE_COLS)
                    else FEATURE_COLS_ALLTH)

    acc = accuracy_score(y_true, y_pred)
    cm  = confusion_matrix(y_true, y_pred, labels=STRATEGIES)
    return {"accuracy": acc, "confusion": cm, "importances": imp,
            "y_true": y_true, "y_pred": y_pred}


def run(num_threads: int, label_by: str):
    print(f"\n[SVM] {num_threads}TH  label={label_by}")
    X, y, perf = collect_dataset(OUTPUTS_DIR, num_threads, label_by)
    if X.empty:
        print(f"[SVM] データなし: {OUTPUTS_DIR}/{num_threads}TH")
        return {}

    res = train_and_evaluate(X, y)
    print(f"[SVM] LOO accuracy={res['accuracy']:.3f}")

    out_dir = MODEL_DIR / f"{num_threads}TH"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = label_by

    # Feature importance (permutation)
    fig, ax = plt.subplots(figsize=(10, 5))
    res["importances"].sort_values().plot.barh(ax=ax, color="darkorange")
    ax.set_title(f"SVM Permutation Importance ({num_threads}TH, {tag})", fontsize=13, fontweight="bold")
    _save(fig, out_dir / f"feature_importance_{tag}.png")

    plot_confusion_matrix(res["confusion"], STRATEGIES,
                          f"SVM Confusion Matrix ({num_threads}TH, {tag})",
                          out_dir / f"confusion_matrix_{tag}.png")
    plot_strategy_dist(y, f"Best Strategy Distribution ({num_threads}TH)",
                       out_dir / f"strategy_dist_{tag}.png")

    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{tag}.csv", index=False)
    X.join(perf).to_csv(out_dir / f"dataset_{tag}.csv")
    pd.DataFrame([{"threads": num_threads, "label": label_by,
                   "accuracy": res["accuracy"], "n_samples": len(X)}]).to_csv(
        out_dir / f"performance_{tag}.csv", index=False)
    return res


def main():
    p = argparse.ArgumentParser(description="Sniper NUMA 戦略 SVM 分類器")
    p.add_argument("--threads", help="スレッド数 (2/4/8/16/all)")
    p.add_argument("--label", default="sim_seconds", choices=["sim_seconds", "energy_j"])
    args = p.parse_args()

    targets = THREAD_NUMS if args.threads == "all" else [int(args.threads)]
    for n in targets:
        run(n, args.label)


if __name__ == "__main__":
    main()
