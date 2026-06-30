"""
svm.py
Sniper NUMA スケジューリング戦略 SVM 分類器。
RBF カーネル SVM + StandardScaler。
LOO-CV (Leave-One-Out) で C/gamma をネストチューニング。

Usage:
  python3 svm.py [--all] [--label sim_seconds]
  --all : 全スレッド数（2/4/8/16）＋ ALLTH 結合モデルを実行
          省略時は THREAD_NUMS ごとに個別実行（--threads で絞り込み可能）
  --label : 最適戦略の評価指標 (sim_seconds | energy_j)
  --threads : 特定スレッド数のみ (例: --threads 16)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import warnings

from sklearn.model_selection import LeaveOneOut, GridSearchCV, StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.metrics import confusion_matrix

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from MachineLearning.ml_utils import (
    STRATEGIES, THREAD_NUMS, OUTPUTS_DIR, FEATURE_COLS, FEATURE_COLS_ALLTH,
    collect_dataset, build_allth_data,
    plot_feature_importance, plot_confusion_matrix, plot_strategy_dist,
    print_per_sample,
)

# 出力先ルート
OUT_ROOT = Path(__file__).parent

# GridSearch 探索グリッド（LOO 内の inner CV は 5-fold、訓練数≧5 なら十分）
PARAM_GRID = {
    "svm__C":     [0.01, 0.1, 1, 10, 100],
    "svm__gamma": ["scale", "auto", 0.01, 0.1, 1],
}


# ── LOO-CV ──────────────────────────────────────────────────────
def _make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("svm",    SVC(kernel="rbf", class_weight="balanced")),
    ])


def train_and_evaluate(X: pd.DataFrame, y: pd.Series) -> dict:
    classes = sorted(y.unique())
    loo     = LeaveOneOut()
    y_true, y_pred = [], []

    # クラスが 1 種類しかない場合は全て多数決で返す（8TH/Scatter 独占など）
    if len(classes) == 1:
        majority = classes[0]
        y_true   = y.tolist()
        y_pred   = [majority] * len(y)
        cm       = confusion_matrix(y_true, y_pred, labels=classes)
        feat_imp = _permutation_importance(X, y)
        acc      = 1.0
        return {
            "accuracy":    acc,
            "confusion":   cm,
            "importances": pd.Series(feat_imp, index=X.columns.tolist()),
            "classes":     classes,
            "y_true":      y_true,
            "y_pred":      y_pred,
        }

    for train_idx, test_idx in loo.split(X):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr        = y.iloc[train_idx]

        # 訓練セットのクラスが 1 種類 → fit 不可なので多数決を返す
        n_classes_tr = y_tr.nunique()
        if n_classes_tr < 2:
            pred = y_tr.mode()[0]
        else:
            min_cls_cnt = int(y_tr.value_counts().min())
            n_splits    = max(2, min(5, min_cls_cnt))
            pipe = _make_pipe()
            cv   = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                gs = GridSearchCV(pipe, PARAM_GRID, cv=cv,
                                  scoring="accuracy", n_jobs=-1, refit=True)
                gs.fit(X_tr, y_tr)
            pred = gs.predict(X_te)[0]

        y_true.append(y.iloc[test_idx[0]])
        y_pred.append(pred)

    acc = sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)
    cm  = confusion_matrix(y_true, y_pred, labels=classes)

    # 全データでモデルを再学習して特徴量重要度の代替（permutation importance 相当）
    # SVM は線形でないため linear SVC 重みではなく permutation 近似を利用
    feat_imp = _permutation_importance(X, y)

    return {
        "accuracy":    acc,
        "confusion":   cm,
        "importances": pd.Series(feat_imp, index=X.columns.tolist()),
        "classes":     classes,
        "y_true":      y_true,
        "y_pred":      y_pred,
    }


def _permutation_importance(X: pd.DataFrame, y: pd.Series) -> np.ndarray:
    """全データで訓練した SVM の Permutation Importance（近似）。"""
    if y.nunique() < 2:
        return np.ones(X.shape[1]) / X.shape[1]
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("svm",    SVC(kernel="rbf", class_weight="balanced")),
    ])
    pipe.fit(X, y)
    base_acc = (pipe.predict(X) == y.values).mean()
    imps = []
    rng  = np.random.default_rng(0)
    X_np = X.values.copy()
    for col in range(X_np.shape[1]):
        X_perm           = X_np.copy()
        X_perm[:, col]   = rng.permutation(X_perm[:, col])
        X_df             = pd.DataFrame(X_perm, columns=X.columns)
        perm_acc         = (pipe.predict(X_df) == y.values).mean()
        imps.append(max(0.0, base_acc - perm_acc))
    total = sum(imps) or 1.0
    return np.array([v / total for v in imps])


# ── 保存・表示 ──────────────────────────────────────────────────
def save_outputs(res: dict, X: pd.DataFrame, y: pd.Series,
                 out_dir: Path, label: str, tag: str):
    out_dir.mkdir(parents=True, exist_ok=True)

    plot_feature_importance(
        res["importances"],
        f"SVM Feature Importance ({tag}, label={label})",
        out_dir / f"feature_importance_{label}.png",
    )
    plot_confusion_matrix(
        res["confusion"], res["classes"],
        f"SVM Confusion Matrix ({tag}, label={label})",
        out_dir / f"confusion_matrix_{label}.png",
    )
    plot_strategy_dist(
        y, f"Strategy Distribution ({tag})",
        out_dir / f"strategy_dist_{label}.png",
    )
    ds = X.copy()
    ds["best_strategy"] = y
    ds.to_csv(out_dir / f"dataset_{label}.csv")
    print(f"[SVM] 出力: {out_dir}")


# ── エントリポイント ─────────────────────────────────────────────
def run_per_thread(num_threads: int, label: str):
    tag = f"{num_threads}TH"
    print(f"\n[SVM] {tag}  label={label}")

    X, y, _ = collect_dataset(OUTPUTS_DIR, num_threads, label)
    if X.empty:
        print(f"[SVM] {tag}: データなし")
        return

    print(f"[SVM] サンプル数={len(X)}  特徴量={len(X.columns)}")
    print(f"[SVM] 戦略分布:\n{y.value_counts().to_string()}")

    res = train_and_evaluate(X, y)
    print_per_sample(X.index.tolist(), res["y_true"], res["y_pred"])
    save_outputs(res, X, y, OUT_ROOT / tag, label, tag)


def run_allth(label: str):
    print(f"\n[SVM-ALLTH] label={label}")
    X_all, y_all = build_allth_data(label)
    if X_all.empty:
        print("[SVM-ALLTH] データなし")
        return

    print(f"[SVM-ALLTH] サンプル数={len(X_all)}")
    res = train_and_evaluate(X_all, y_all)
    print_per_sample(X_all.index.tolist(), res["y_true"], res["y_pred"], header="Sample")
    save_outputs(res, X_all, y_all, OUT_ROOT / "ALLTH", label, "ALLTH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all",     action="store_true", help="全スレッド数 + ALLTH")
    ap.add_argument("--label",   default="sim_seconds")
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()

    if args.all:
        for n in THREAD_NUMS:
            run_per_thread(n, args.label)
        run_allth(args.label)
    elif args.threads:
        run_per_thread(args.threads, args.label)
    else:
        for n in THREAD_NUMS:
            run_per_thread(n, args.label)


if __name__ == "__main__":
    main()
