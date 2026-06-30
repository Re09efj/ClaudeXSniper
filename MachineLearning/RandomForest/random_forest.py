"""
random_forest.py
Sniper NUMA 配置戦略の自動選択モデル (Random Forest, スレッド数別)

使い方:
    python random_forest.py --threads 4
    python random_forest.py --threads 8 --label energy_j
    python random_forest.py --all

特徴量: Packed ランを「プロファイリング基準実行」として抽出（Sniper SQLite3）
ラベル: sim_seconds or energy_j が最小の戦略
評価:   Leave-One-Out CV (サンプル数13のため)
出力:   MachineLearning/RandomForest/{N}TH/ 以下に PNG / CSV を保存
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
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from utility.cpu_affinity import get_cpu_map as _get_cpu_map
from utility.stats_reader import P_CORES, E_CORES, NODE0_CPUS
from MachineLearning.ml_utils import (
    parse_stats,
    FEATURE_COLS,
    FEATURE_COLS_ALLTH,
)

# ── CPU トポロジ ─────────────────────────────────────────────
ALL_P_CORES = sorted(P_CORES)
ALL_E_CORES = sorted(E_CORES)
STRATEGIES  = ["Packed", "Scatter", "HPO", "EPO"]
THREAD_NUMS = [2, 4, 8, 16]

ML_DIR      = Path(__file__).parent
OUTPUTS_DIR = ML_DIR.parent.parent / "Outputs" / "sizeS"


def active_cores_for_map(cpu_map: list, num_threads: int) -> tuple[list[int], list[int]]:
    """cpu_map の先頭 num_threads 個から P/E コアインデックスを返す。"""
    active = cpu_map[:num_threads]
    p = [i for i, c in enumerate(active) if c in P_CORES]
    e = [i for i, c in enumerate(active) if c in E_CORES]
    return p, e




# ── データセット収集 ─────────────────────────────────────────
def collect_dataset(
    outputs_dir: Path,
    num_threads: int,
    label_by: str,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """
    outputs_dir/{N}TH/ 以下の Sniper 出力から学習データを収集する。

    Returns: (X, y, perf_df)
      X      : 特徴量 DataFrame (index=workload名)
      y      : ラベル Series (最適戦略名)
      perf_df: 性能値 DataFrame (index=workload名, columns=STRATEGIES)
    """
    suffix     = f"{num_threads}TH"
    thread_dir = outputs_dir / suffix
    if not thread_dir.exists():
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    # ワークロードごとに戦略ディレクトリをグループ化
    wl_dirs: dict[str, dict[str, Path]] = {}
    for d in sorted(thread_dir.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 4:
            continue
        workload  = parts[0]
        strategy  = parts[2]
        if strategy not in STRATEGIES:
            continue
        wl_dirs.setdefault(workload, {})[strategy] = d

    rows_X, rows_y, rows_perf, names = [], [], [], []

    for wl, sdirs in wl_dirs.items():
        if "Packed" not in sdirs:
            continue
        if not all(s in sdirs for s in STRATEGIES):
            continue

        packed_cpu_map = _get_cpu_map("Packed", wl)
        packed_stats   = parse_stats(sdirs["Packed"], num_threads, cpu_map=packed_cpu_map)
        if not packed_stats or packed_stats.get("sim_seconds", 0) == 0:
            continue

        perf_row = {}
        for s in STRATEGIES:
            s_cpu_map = _get_cpu_map(s, wl)
            s_stats   = parse_stats(sdirs[s], num_threads, cpu_map=s_cpu_map) or {}
            perf_row[s] = s_stats.get(label_by, float("inf"))

        best_val = min(perf_row.values())
        # 最良値の 2% 以内は同率とみなし、STRATEGIES リスト先頭を優先
        best = min(
            (s for s in STRATEGIES if perf_row[s] <= best_val * 1.02),
            key=lambda s: STRATEGIES.index(s),
        )

        rows_X.append([packed_stats.get(c, 0.0) for c in FEATURE_COLS])
        rows_y.append(best)
        rows_perf.append(perf_row)
        names.append(wl)

    if not names:
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    X    = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS)
    y    = pd.Series(rows_y, index=names, name="best_strategy")
    perf = pd.DataFrame(rows_perf, index=names, columns=STRATEGIES)
    return X, y, perf


# ── モデル訓練・評価 ─────────────────────────────────────────
def train_and_evaluate(X: pd.DataFrame, y: pd.Series) -> dict:
    loo = LeaveOneOut()
    y_true, y_pred, importances = [], [], []

    for train_idx, test_idx in loo.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train = y.iloc[train_idx]

        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            y_true.append(y.iloc[test_idx[0]])
            continue

        clf = RandomForestClassifier(
            n_estimators=200, max_depth=None,
            class_weight="balanced", random_state=42,
        )
        clf.fit(X_train, y_train)
        y_pred.append(clf.predict(X_test)[0])
        y_true.append(y.iloc[test_idx[0]])
        importances.append(clf.feature_importances_)

    acc = accuracy_score(y_true, y_pred)
    cm  = confusion_matrix(y_true, y_pred, labels=STRATEGIES)
    feat_cols = X.columns.tolist()
    avg_imp = np.mean(importances, axis=0) if importances else np.zeros(len(feat_cols))
    return {
        "accuracy":    acc,
        "confusion":   cm,
        "importances": pd.Series(avg_imp, index=feat_cols),
        "y_true":      y_true,
        "y_pred":      y_pred,
    }


# ── プロット ─────────────────────────────────────────────────
def _save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_feature_importance(imp: pd.Series, title: str, path: Path):
    fig, ax = plt.subplots(figsize=(10, 5))
    imp.sort_values().plot.barh(ax=ax, color="steelblue")
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Importance")
    _save(fig, path)
    print(f"[RF] 特徴量重要度: {path}")


def plot_confusion_matrix(cm: np.ndarray, classes: list, title: str, path: Path):
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
    _save(fig, path)
    print(f"[RF] 混同行列: {path}")


def plot_strategy_dist(y: pd.Series, title: str, path: Path):
    fig, ax = plt.subplots(figsize=(6, 4))
    y.value_counts().plot.bar(ax=ax, color="steelblue", rot=0)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Count")
    _save(fig, path)
    print(f"[RF] 戦略分布: {path}")


# ── メイン実験 ───────────────────────────────────────────────
def run(num_threads: int, label_by: str):
    print(f"\n[RF] {num_threads}TH  label={label_by}")

    X, y, perf = collect_dataset(OUTPUTS_DIR, num_threads, label_by)
    if X.empty:
        print(f"[RF] データなし: {OUTPUTS_DIR}/{num_threads}TH")
        return {}

    print(f"[RF] サンプル数={len(X)}  特徴量={len(FEATURE_COLS)}")
    print(f"[RF] 戦略分布:\n{y.value_counts()}")

    res = train_and_evaluate(X, y)
    acc = res["accuracy"]
    print(f"[RF] LOO accuracy={acc:.3f}  ({int(acc*len(X))}/{len(X)})")
    print(f"\n  {'Workload':<10} {'True':<10} {'Pred':<10} {'OK?'}")
    print(f"  {'-'*42}")
    for wl, t, p in zip(X.index, res["y_true"], res["y_pred"]):
        ok = "✓" if t == p else "✗"
        print(f"  {wl:<10} {t:<10} {p:<10} {ok}")

    out_dir = ML_DIR / f"{num_threads}TH"
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = label_by
    plot_feature_importance(
        res["importances"],
        f"Feature Importance ({num_threads}TH, {tag})",
        out_dir / f"feature_importance_{tag}.png",
    )
    plot_confusion_matrix(
        res["confusion"], STRATEGIES,
        f"Confusion Matrix ({num_threads}TH, {tag})",
        out_dir / f"confusion_matrix_{tag}.png",
    )
    plot_strategy_dist(
        y, f"Best Strategy Distribution ({num_threads}TH)",
        out_dir / f"strategy_dist_{tag}.png",
    )

    # CSV 保存
    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{tag}.csv", index=False)

    # dataset: features + best_strategy（POSM/Regression が読み込む形式）
    ds_df = X.copy()
    ds_df["best_strategy"] = y
    ds_df.to_csv(out_dir / f"dataset_{tag}.csv")
    print(f"[RF] データセット保存: {out_dir}/dataset_{tag}.csv")

    # performance: ワークロード別各戦略の実測値（POSM/Regression が読み込む形式）
    perf.to_csv(out_dir / f"performance_{tag}.csv")

    return res


def _build_allth_data(label_by: str):
    rows_X, rows_y, names = [], [], []
    for n in THREAD_NUMS:
        X_n, y_n, _ = collect_dataset(OUTPUTS_DIR, n, label_by)
        if X_n.empty:
            continue
        X_aug = X_n.copy()
        X_aug.insert(0, "num_threads", n)
        for i, idx in enumerate(X_aug.index):
            rows_X.append(X_aug.iloc[i].values)
            rows_y.append(y_n.iloc[i])
            names.append(f"{idx}_{n}TH")
    if not names:
        return pd.DataFrame(), pd.Series(dtype=str)
    X_all = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS_ALLTH)
    y_all = pd.Series(rows_y, index=names, name="best_strategy")
    return X_all, y_all


def run_allth(label_by: str) -> dict:
    print(f"\n[RF-ALLTH] label={label_by}")
    X_all, y_all = _build_allth_data(label_by)
    if X_all.empty:
        print("[RF-ALLTH] データなし")
        return {}

    print(f"[RF-ALLTH] サンプル数={len(X_all)}")
    res = train_and_evaluate(X_all, y_all)
    acc = res["accuracy"]
    print(f"[RF-ALLTH] LOO accuracy={acc:.3f}  ({int(acc*len(X_all))}/{len(X_all)})")
    print(f"\n  {'Sample':<18} {'True':<10} {'Pred':<10} {'OK?'}")
    print(f"  {'-'*50}")
    for name, t, p in zip(X_all.index, res["y_true"], res["y_pred"]):
        ok = "✓" if t == p else "✗"
        print(f"  {name:<18} {t:<10} {p:<10} {ok}")

    out_dir = ML_DIR / "ALLTH"
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = label_by
    plot_feature_importance(
        res["importances"].reindex(FEATURE_COLS_ALLTH, fill_value=0),
        f"Feature Importance (All Threads, {tag})",
        out_dir / f"feature_importance_{tag}.png",
    )
    plot_confusion_matrix(
        res["confusion"], STRATEGIES,
        f"Confusion Matrix (All Threads, {tag})",
        out_dir / f"confusion_matrix_{tag}.png",
    )
    plot_strategy_dist(
        y_all, f"Best Strategy Distribution (All Threads)",
        out_dir / f"strategy_dist_{tag}.png",
    )
    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{tag}.csv", index=False)

    # dataset: features + best_strategy
    ds_df = X_all.copy()
    ds_df["best_strategy"] = y_all
    ds_df.to_csv(out_dir / f"dataset_{tag}.csv")

    # performance ALLTH: 各スレッド設定ごとのワークロード別実測値を結合
    perf_frames = []
    for n in THREAD_NUMS:
        _, _, perf_n = collect_dataset(OUTPUTS_DIR, n, label_by)
        if perf_n.empty:
            continue
        perf_n = perf_n.copy()
        perf_n.index = [f"{wl}_{n}TH" for wl in perf_n.index]
        perf_frames.append(perf_n)
    if perf_frames:
        pd.concat(perf_frames).to_csv(out_dir / f"performance_{tag}.csv")

    return res


def main():
    p = argparse.ArgumentParser(description="Sniper NUMA 戦略 Random Forest 分類器")
    p.add_argument("--threads", type=int, choices=[2, 4, 8, 16],
                   help="スレッド数 (未指定時は --all が必要)")
    p.add_argument("--label", default="sim_seconds",
                   choices=["sim_seconds", "energy_j"],
                   help="最適化対象 (default: sim_seconds)")
    p.add_argument("--all", action="store_true", help="全スレッド数を実行")
    args = p.parse_args()

    if args.all:
        for n in THREAD_NUMS:
            run(n, args.label)
        run_allth(args.label)
    elif args.threads:
        run(args.threads, args.label)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
