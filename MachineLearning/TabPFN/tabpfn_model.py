"""
tabpfn_model.py
NUMA 配置戦略の自動選択モデル - TabPFN 版

使い方:
    python tabpfn_model.py               # 全スレッド + ALLTH
    python tabpfn_model.py --threads 4
    python tabpfn_model.py --label energy_j
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut

try:
    from tabpfn_client import TabPFNClassifier, set_access_token
    import os
    _token = os.environ.get("TABPFN_TOKEN", "tabpfn_sk_lEBsJpNhQfIxGUMGBHFQZhmWNskPUJy2jUeS3omRGZw")
    set_access_token(_token)
    HAS_TABPFN = True
except ImportError:
    HAS_TABPFN = False
    print("[TabPFN] tabpfn-client が見つかりません: pip install tabpfn-client")

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
MODEL_DIR   = Path(__file__).parent


def make_clf():
    return TabPFNClassifier()


def train_and_evaluate(X: pd.DataFrame, y: pd.Series) -> dict:
    if not HAS_TABPFN:
        return {}

    loo = LeaveOneOut()
    y_true, y_pred = [], []
    n_total = len(X)

    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        wl_name = X.index[test_idx[0]]
        print(f"  [{fold_i+1:2d}/{n_total}] {wl_name}", flush=True)

        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])

        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue

        import time
        for attempt in range(5):
            try:
                clf = make_clf()
                clf.fit(X.iloc[train_idx], y_train)
                y_pred.append(clf.predict(X.iloc[test_idx])[0])
                break
            except Exception as e:
                if attempt < 4:
                    print(f"  [retry {attempt+1}/4] {e.__class__.__name__}", flush=True)
                    time.sleep(10 * (attempt + 1))
                else:
                    raise

    # 最終モデルで permutation importance
    feat_cols = FEATURE_COLS if X.shape[1] == len(FEATURE_COLS) else FEATURE_COLS_ALLTH
    if y.nunique() < 2:
        imp = pd.Series(0.0, index=feat_cols)
    else:
        clf_full = make_clf()
        clf_full.fit(X, y)
        r = permutation_importance(clf_full, X, y, n_repeats=10, random_state=42)
        imp = pd.Series(r.importances_mean, index=feat_cols)

    acc = accuracy_score(y_true, y_pred)
    cm  = confusion_matrix(y_true, y_pred, labels=STRATEGIES)
    return {"accuracy": acc, "confusion": cm, "importances": imp,
            "y_true": y_true, "y_pred": y_pred}


def run(num_threads: int, label_by: str):
    print(f"\n[TabPFN] {num_threads}TH  label={label_by}")
    X, y, perf = collect_dataset(OUTPUTS_DIR, num_threads, label_by)
    if X.empty:
        print(f"[TabPFN] データなし: {OUTPUTS_DIR}/{num_threads}TH")
        return {}
    if not HAS_TABPFN:
        return {}

    res = train_and_evaluate(X, y)
    if not res:
        return {}
    print(f"[TabPFN] LOO accuracy={res['accuracy']:.3f}  ({int(res['accuracy']*len(y))}/{len(y)})")

    out_dir = MODEL_DIR / f"{num_threads}TH"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = label_by

    fig, ax = plt.subplots(figsize=(10, 5))
    res["importances"].sort_values().plot.barh(ax=ax, color="mediumpurple")
    ax.set_title(f"TabPFN Permutation Importance ({num_threads}TH, {tag})", fontsize=13, fontweight="bold")
    _save(fig, out_dir / f"feature_importance_{tag}.png")

    plot_confusion_matrix(res["confusion"], STRATEGIES,
                          f"TabPFN Confusion Matrix ({num_threads}TH, {tag})",
                          out_dir / f"confusion_matrix_{tag}.png", tag="TabPFN")
    plot_strategy_dist(y, f"Best Strategy Distribution ({num_threads}TH)",
                       out_dir / f"strategy_dist_{tag}.png", tag="TabPFN")

    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{tag}.csv", index=False)
    X.join(perf).to_csv(out_dir / f"dataset_{tag}.csv")
    pd.DataFrame([{"threads": num_threads, "label": label_by,
                   "accuracy": res["accuracy"], "n_samples": len(X)}]).to_csv(
        out_dir / f"performance_{tag}.csv", index=False)
    return res


def run_allth(label_by: str):
    print(f"\n[TabPFN-ALLTH] label={label_by}")
    X, y = _build_allth_data(label_by)
    if X.empty or not HAS_TABPFN:
        return {}

    res = train_and_evaluate(X, y)
    if not res:
        return {}
    print(f"[TabPFN-ALLTH] LOO accuracy={res['accuracy']:.3f}  ({int(res['accuracy']*len(y))}/{len(y)})")

    out_dir = MODEL_DIR / "ALLTH"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = label_by

    fig, ax = plt.subplots(figsize=(10, 5))
    res["importances"].sort_values().plot.barh(ax=ax, color="mediumpurple")
    ax.set_title(f"TabPFN Permutation Importance (ALLTH, {tag})", fontsize=13, fontweight="bold")
    _save(fig, out_dir / f"feature_importance_{tag}.png")

    plot_confusion_matrix(res["confusion"], STRATEGIES,
                          f"TabPFN Confusion Matrix (ALLTH, {tag})",
                          out_dir / f"confusion_matrix_{tag}.png", tag="TabPFN")
    plot_strategy_dist(y, "Best Strategy Distribution (ALLTH)",
                       out_dir / f"strategy_dist_{tag}.png", tag="TabPFN")

    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{tag}.csv", index=False)
    pd.DataFrame([{"threads": "ALL", "label": label_by,
                   "accuracy": res["accuracy"], "n_samples": len(X)}]).to_csv(
        out_dir / f"performance_{tag}.csv", index=False)
    return res


def main():
    p = argparse.ArgumentParser(description="Sniper NUMA 戦略 TabPFN 分類器")
    p.add_argument("--threads", help="スレッド数 (2/4/8/16/allth)")
    p.add_argument("--label", default="sim_seconds", choices=["sim_seconds", "energy_j"])
    args = p.parse_args()

    if args.threads in ("allth", "ALLTH"):
        run_allth(args.label)
    elif args.threads:
        run(int(args.threads), args.label)
    else:
        for n in THREAD_NUMS:
            run(n, args.label)
        run_allth(args.label)


if __name__ == "__main__":
    main()
