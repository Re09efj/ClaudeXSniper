"""
train_combined.py
sizeS + sizeW 結合データで RF / SVM / XGB / TabPFN を ALLTH 再訓練する。

出力先: MachineLearning/{RF,SVM,XGBoost,TabPFN}/ALLTH_SW/

使い方:
    python train_combined.py
    python train_combined.py --label energy_j
    python train_combined.py --model rf
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance as sk_perm_imp
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import SVC

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from MachineLearning.ml_utils import (
    STRATEGIES,
    FEATURE_COLS,
    FEATURE_COLS_ALLTH_MULTI,
    build_allth_data,
    save_fig,
)

OUTPUTS_DIRS = [
    ROOT / "Outputs" / "sizeS",
    ROOT / "Outputs" / "sizeW",
]
ML_DIR = Path(__file__).parent
OUT_SUFFIX = "ALLTH_SW"


# ── 共通プロット ────────────────────────────────────────────────────
def _plot_imp(imp: pd.Series, title: str, path: Path, color: str):
    fig, ax = plt.subplots(figsize=(10, 6))
    imp.sort_values().plot.barh(ax=ax, color=color)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Importance")
    save_fig(fig, path)


def _plot_cm(cm: np.ndarray, title: str, path: Path, tag: str):
    classes = STRATEGIES
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(classes)))
    ax.set_yticks(range(len(classes)))
    ax.set_xticklabels(classes, rotation=45, ha="right")
    ax.set_yticklabels(classes)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_title(title, fontsize=12, fontweight="bold")
    for i in range(len(classes)):
        for j in range(len(classes)):
            ax.text(j, i, cm[i, j], ha="center", va="center",
                    color="white" if cm[i, j] > cm.max() / 2 else "black")
    fig.colorbar(im, ax=ax)
    save_fig(fig, path)
    print(f"[{tag}] 混同行列: {path}")


def _plot_dist(y: pd.Series, title: str, path: Path, tag: str):
    fig, ax = plt.subplots(figsize=(6, 4))
    y.value_counts().reindex(STRATEGIES, fill_value=0).plot.bar(
        ax=ax, color="steelblue", rot=0)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Count")
    save_fig(fig, path)
    print(f"[{tag}] 戦略分布: {path}")


def _save_results(res: dict, X: pd.DataFrame, y: pd.Series,
                  out_dir: Path, tag: str, label_by: str, color: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    _plot_imp(res["importances"], f"{tag} Feature Importance ({OUT_SUFFIX}, {label_by})",
              out_dir / f"feature_importance_{label_by}.png", color)
    _plot_cm(res["confusion"],
             f"{tag} Confusion Matrix ({OUT_SUFFIX}, {label_by})",
             out_dir / f"confusion_matrix_{label_by}.png", tag)
    _plot_dist(y, f"Best Strategy Distribution ({OUT_SUFFIX})",
               out_dir / f"strategy_dist_{label_by}.png", tag)
    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{label_by}.csv", index=False)
    pd.DataFrame([{"threads": "ALL", "sizes": "SW", "label": label_by,
                   "accuracy": res["accuracy"], "n_samples": len(X)}]).to_csv(
        out_dir / f"performance_{label_by}.csv", index=False)
    print(f"[{tag}] LOO accuracy={res['accuracy']:.3f}  ({int(res['accuracy']*len(y))}/{len(y)})")


# ── Random Forest ───────────────────────────────────────────────────
def run_rf(X: pd.DataFrame, y: pd.Series, label_by: str):
    print(f"\n[RF-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred, importances = [], [], []
    for train_idx, test_idx in loo.split(X):
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        clf = RandomForestClassifier(n_estimators=300, max_depth=None,
                                     class_weight="balanced", random_state=42)
        clf.fit(X.iloc[train_idx], y_train)
        y_pred.append(clf.predict(X.iloc[test_idx])[0])
        importances.append(clf.feature_importances_)

    avg_imp = np.mean(importances, axis=0) if importances else np.zeros(X.shape[1])
    res = {
        "accuracy":    accuracy_score(y_true, y_pred),
        "confusion":   confusion_matrix(y_true, y_pred, labels=STRATEGIES),
        "importances": pd.Series(avg_imp, index=X.columns.tolist()),
    }
    _save_results(res, X, y, ML_DIR / "RandomForest" / OUT_SUFFIX, "RF", label_by, "steelblue")


# ── SVM ────────────────────────────────────────────────────────────
def run_svm(X: pd.DataFrame, y: pd.Series, label_by: str):
    print(f"\n[SVM-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred = [], []

    def make_pipe():
        return Pipeline([
            ("scaler", StandardScaler()),
            ("svm", SVC(kernel="rbf", C=10.0, gamma="scale",
                        class_weight="balanced", random_state=42,
                        decision_function_shape="ovr")),
        ])

    for train_idx, test_idx in loo.split(X):
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        pipe = make_pipe()
        pipe.fit(X.iloc[train_idx], y_train)
        y_pred.append(pipe.predict(X.iloc[test_idx])[0])

    if y.nunique() < 2:
        imp = pd.Series(0.0, index=X.columns.tolist())
    else:
        pipe_full = make_pipe()
        pipe_full.fit(X, y)
        r = sk_perm_imp(pipe_full, X, y, n_repeats=10, random_state=42)
        imp = pd.Series(r.importances_mean, index=X.columns.tolist())

    res = {
        "accuracy":    accuracy_score(y_true, y_pred),
        "confusion":   confusion_matrix(y_true, y_pred, labels=STRATEGIES),
        "importances": imp,
    }
    _save_results(res, X, y, ML_DIR / "SVM" / OUT_SUFFIX, "SVM", label_by, "darkorange")


# ── XGBoost ────────────────────────────────────────────────────────
def run_xgb(X: pd.DataFrame, y: pd.Series, label_by: str):
    try:
        from xgboost import XGBClassifier
    except ImportError:
        print("[XGB] xgboost not installed")
        return

    print(f"\n[XGB-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    le = LabelEncoder()
    le.fit(STRATEGIES)
    y_enc = le.transform(y)

    loo = LeaveOneOut()
    y_true_enc, y_pred_enc = [], []
    n_total = len(X)

    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        wl_name = X.index[test_idx[0]]
        print(f"  [{fold_i+1:3d}/{n_total}] {wl_name}", flush=True)
        y_train = y_enc[train_idx]
        if len(np.unique(y_train)) < 2:
            y_pred_enc.append(y_train[0])
            y_true_enc.append(y_enc[test_idx[0]])
            continue
        clf = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.1,
                            use_label_encoder=False, eval_metric="mlogloss",
                            random_state=42, verbosity=0)
        X_tr = X.iloc[train_idx].copy()
        y_tr = list(y_train)
        for cls in set(range(len(STRATEGIES))) - set(np.unique(y_train)):
            X_tr = pd.concat([X_tr, X_tr.mean().to_frame().T], ignore_index=True)
            y_tr.append(cls)
        clf.fit(X_tr, np.array(y_tr))
        y_pred_enc.append(clf.predict(X.iloc[test_idx])[0])
        y_true_enc.append(y_enc[test_idx[0]])

    y_true = le.inverse_transform(y_true_enc)
    y_pred = le.inverse_transform(y_pred_enc)

    clf_full = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.1,
                             use_label_encoder=False, eval_metric="mlogloss",
                             random_state=42, verbosity=0)
    X_full = X.copy()
    y_full = list(y_enc)
    for cls in set(range(len(STRATEGIES))) - set(np.unique(y_enc)):
        X_full = pd.concat([X_full, X_full.mean().to_frame().T], ignore_index=True)
        y_full.append(cls)
    clf_full.fit(X_full, np.array(y_full))
    imp = pd.Series(clf_full.feature_importances_, index=X.columns.tolist())

    res = {
        "accuracy":    accuracy_score(y_true, y_pred),
        "confusion":   confusion_matrix(y_true, y_pred, labels=STRATEGIES),
        "importances": imp,
    }
    _save_results(res, X, pd.Series(y, index=X.index), ML_DIR / "XGBoost" / OUT_SUFFIX, "XGB", label_by, "forestgreen")


# ── TabPFN ─────────────────────────────────────────────────────────
def run_tabpfn(X: pd.DataFrame, y: pd.Series, label_by: str):
    try:
        from tabpfn_client import TabPFNClassifier, set_access_token
        import os
        token = os.environ.get("TABPFN_TOKEN",
                               "tabpfn_sk_lEBsJpNhQfIxGUMGBHFQZhmWNskPUJy2jUeS3omRGZw")
        set_access_token(token)
    except ImportError:
        print("[TabPFN] tabpfn-client not installed")
        return

    print(f"\n[TabPFN-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred = [], []
    n_total = len(X)

    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        wl_name = X.index[test_idx[0]]
        print(f"  [{fold_i+1:3d}/{n_total}] {wl_name}", flush=True)
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        for attempt in range(5):
            try:
                clf = TabPFNClassifier()
                clf.fit(X.iloc[train_idx], y_train)
                y_pred.append(clf.predict(X.iloc[test_idx])[0])
                break
            except Exception as e:
                if attempt < 4:
                    print(f"  [retry {attempt+1}/4] {e.__class__.__name__}", flush=True)
                    time.sleep(10 * (attempt + 1))
                else:
                    raise

    if y.nunique() < 2:
        imp = pd.Series(0.0, index=X.columns.tolist())
    else:
        clf_full = TabPFNClassifier()
        clf_full.fit(X, y)
        r = sk_perm_imp(clf_full, X, y, n_repeats=10, random_state=42)
        imp = pd.Series(r.importances_mean, index=X.columns.tolist())

    res = {
        "accuracy":    accuracy_score(y_true, y_pred),
        "confusion":   confusion_matrix(y_true, y_pred, labels=STRATEGIES),
        "importances": imp,
    }
    _save_results(res, X, y, ML_DIR / "TabPFN" / OUT_SUFFIX, "TabPFN", label_by, "mediumpurple")


# ── メイン ─────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="sizeS+W 結合 ALLTH 再訓練")
    p.add_argument("--label", default="sim_seconds",
                   choices=["sim_seconds", "energy_j"])
    p.add_argument("--model", default="all",
                   choices=["all", "rf", "svm", "xgb", "tabpfn"])
    args = p.parse_args()

    print(f"=== 結合訓練: sizeS + sizeW  label={args.label} ===")
    X, y = build_allth_data(args.label, OUTPUTS_DIRS)
    if X.empty:
        print("データが取得できませんでした。Outputs/sizeS と sizeW を確認してください。")
        sys.exit(1)

    print(f"データロード完了: {len(X)} サンプル  特徴量={X.shape[1]}")
    print(f"列: {list(X.columns)}")
    print(f"戦略分布:\n{y.value_counts()}\n")

    if args.model in ("all", "rf"):
        run_rf(X.copy(), y.copy(), args.label)
    if args.model in ("all", "svm"):
        run_svm(X.copy(), y.copy(), args.label)
    if args.model in ("all", "xgb"):
        run_xgb(X.copy(), y.copy(), args.label)
    if args.model in ("all", "tabpfn"):
        run_tabpfn(X.copy(), y.copy(), args.label)

    print(f"\n=== 完了 ===  出力: {ML_DIR}/*/ALLTH_SW/")


if __name__ == "__main__":
    main()
