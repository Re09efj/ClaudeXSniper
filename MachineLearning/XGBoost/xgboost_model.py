"""
xgboost_model.py
NUMA 配置戦略の自動選択モデル - XGBoost 版

使い方:
    python xgboost_model.py --threads 4
    python xgboost_model.py --all --label energy_j
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
from sklearn.preprocessing import LabelEncoder

try:
    from xgboost import XGBClassifier
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    print("[XGBoost] xgboost パッケージが見つかりません: pip install xgboost")

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


def train_and_evaluate(X: pd.DataFrame, y: pd.Series) -> dict:
    if not HAS_XGBOOST:
        return {}

    le = LabelEncoder()
    le.fit(STRATEGIES)
    y_enc = le.transform(y)

    loo = LeaveOneOut()
    y_true_enc, y_pred_enc = [], []

    for train_idx, test_idx in loo.split(X):
        y_train = y_enc[train_idx]
        if len(np.unique(y_train)) < 2:
            y_pred_enc.append(y_train[0])
            y_true_enc.append(y_enc[test_idx[0]])
            continue

        clf = XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.1,
            use_label_encoder=False, eval_metric="mlogloss",
            random_state=42, verbosity=0,
        )
        clf.fit(X.iloc[train_idx], y_train)
        y_pred_enc.append(clf.predict(X.iloc[test_idx])[0])
        y_true_enc.append(y_enc[test_idx[0]])

    y_true = le.inverse_transform(y_true_enc)
    y_pred = le.inverse_transform(y_pred_enc)

    # 最終モデルで feature importance
    clf_full = XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.1,
        use_label_encoder=False, eval_metric="mlogloss",
        random_state=42, verbosity=0,
    )
    clf_full.fit(X, y_enc)
    feat_cols = FEATURE_COLS if X.shape[1] == len(FEATURE_COLS) else FEATURE_COLS_ALLTH
    imp = pd.Series(clf_full.feature_importances_, index=feat_cols)

    acc = accuracy_score(y_true, y_pred)
    cm  = confusion_matrix(y_true, y_pred, labels=STRATEGIES)
    return {"accuracy": acc, "confusion": cm, "importances": imp,
            "y_true": list(y_true), "y_pred": list(y_pred)}


def run(num_threads: int, label_by: str):
    print(f"\n[XGB] {num_threads}TH  label={label_by}")
    X, y, perf = collect_dataset(OUTPUTS_DIR, num_threads, label_by)
    if X.empty:
        print(f"[XGB] データなし: {OUTPUTS_DIR}/{num_threads}TH")
        return {}
    if not HAS_XGBOOST:
        return {}

    res = train_and_evaluate(X, y)
    if not res:
        return {}
    print(f"[XGB] LOO accuracy={res['accuracy']:.3f}")

    out_dir = MODEL_DIR / f"{num_threads}TH"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = label_by

    fig, ax = plt.subplots(figsize=(10, 5))
    res["importances"].sort_values().plot.barh(ax=ax, color="forestgreen")
    ax.set_title(f"XGBoost Feature Importance ({num_threads}TH, {tag})", fontsize=13, fontweight="bold")
    _save(fig, out_dir / f"feature_importance_{tag}.png")

    plot_confusion_matrix(res["confusion"], STRATEGIES,
                          f"XGBoost Confusion Matrix ({num_threads}TH, {tag})",
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
    p = argparse.ArgumentParser(description="Sniper NUMA 戦略 XGBoost 分類器")
    p.add_argument("--threads", help="スレッド数 (2/4/8/16/all)")
    p.add_argument("--label", default="sim_seconds", choices=["sim_seconds", "energy_j"])
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    if args.all or args.threads == "all":
        for n in THREAD_NUMS:
            run(n, args.label)
    elif args.threads:
        run(int(args.threads), args.label)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
