"""
summary.py
全スレッド数 (2/4/8/16TH) のランダムフォレスト結果を横断比較する。

使い方:
    python summary.py                   # sim_seconds 基準
    python summary.py --label energy_j  # energy_j 基準
"""

import argparse
from pathlib import Path

import matplotlib
import matplotlib.cm  # noqa: F401 (needed for colormaps access)
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ClaudeX.MachineLearning.RandomForest.random_forest import run, run_allth, STRATEGIES, FEATURE_COLS, THREAD_NUMS

ML_DIR = Path(__file__).parent


def plot_accuracy_comparison(records: list[dict], label_by: str, out_dir: Path):
    """スレッド数別 LOO-CV 精度の棒グラフ。"""
    fig, ax = plt.subplots(figsize=(7, 4))
    xs = [f"{r['num_threads']}TH" for r in records]
    accs = [r["accuracy"] for r in records]
    bars = ax.bar(xs, accs, color=["#3498db","#2ecc71","#e74c3c","#f39c12"], width=0.5)
    ax.set_ylim(0, 1.0)
    ax.axhline(y=0.5, color="gray", ls="--", alpha=0.6, label="random baseline (4-class)")
    for bar, acc, r in zip(bars, accs, records):
        n = r["n_samples"]
        ax.text(bar.get_x() + bar.get_width()/2,
                acc + 0.02,
                f"{acc:.0%}\n({int(acc*n)}/{n})",
                ha="center", va="bottom", fontsize=10)
    ax.set_ylabel("LOO-CV Accuracy")
    ax.set_title(f"LOO-CV Accuracy by Thread Count  (label={label_by})")
    ax.legend()
    path = out_dir / f"accuracy_comparison_{label_by}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVE] {path.name}")


def plot_importance_heatmap(records: list[dict], label_by: str, out_dir: Path):
    """特徴量重要度のスレッド数 × 特徴量 ヒートマップ。"""
    rows = {f"{r['num_threads']}TH": r["importances"] for r in records}
    df = pd.DataFrame(rows, index=FEATURE_COLS).T  # shape: (4, 9)

    fig, ax = plt.subplots(figsize=(11, 4))
    im = ax.imshow(df.values, aspect="auto", cmap="YlOrRd", vmin=0)
    plt.colorbar(im, ax=ax, shrink=0.8)
    ax.set_xticks(range(len(FEATURE_COLS))); ax.set_xticklabels(FEATURE_COLS, rotation=45, ha="right")
    ax.set_yticks(range(len(df))); ax.set_yticklabels(df.index)
    ax.set_title(f"Feature Importance Heatmap (label={label_by})")
    for i in range(len(df)):
        for j in range(len(FEATURE_COLS)):
            ax.text(j, i, f"{df.values[i, j]:.2f}", ha="center", va="center", fontsize=8)
    path = out_dir / f"importance_heatmap_{label_by}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVE] {path.name}")


def plot_strategy_dist_grid(records: list[dict], label_by: str, out_dir: Path):
    """戦略分布を 1×4 のサブプロットで並べる。"""
    fig, axes = plt.subplots(1, 4, figsize=(14, 4), sharey=True)
    colors = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12"]
    for ax, r in zip(axes, records):
        counts = [r["class_dist"].get(s, 0) for s in STRATEGIES]
        bars = ax.bar(STRATEGIES, counts, color=colors)
        ax.set_title(f"{r['num_threads']}TH  (acc={r['accuracy']:.0%})")
        ax.set_xticklabels(STRATEGIES, rotation=30, ha="right")
        for bar, c in zip(bars, counts):
            if c > 0:
                ax.text(bar.get_x() + bar.get_width()/2, c + 0.05, str(c),
                        ha="center", va="bottom")
    fig.suptitle(f"Best Strategy Distribution by Thread Count  (label={label_by})")
    axes[0].set_ylabel("Count")
    path = out_dir / f"strategy_grid_{label_by}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVE] {path.name}")


def plot_per_workload_heatmap(records: list[dict], label_by: str, out_dir: Path):
    """
    ワークロード × スレッド数 のヒートマップ。
    各セルに「最良戦略」を色で表示。
    """
    strategy_color = {
        "Packed":  0,
        "Scatter": 1,
        "HPO":     2,
        "MPO":     3,
        None:      -1,
    }
    # ワークロード一覧 (全スレッド数共通のもの)
    workloads = sorted(set(records[0]["workloads"]))
    n_wl = len(workloads)
    n_th = len(records)

    mat = np.full((n_wl, n_th), -1, dtype=float)
    for j, r in enumerate(records):
        for i, wl in enumerate(workloads):
            if wl in r["workloads"]:
                idx = r["workloads"].index(wl)
                mat[i, j] = strategy_color[r["y_true"][idx]]

    cmap = matplotlib.colormaps["tab10"].resampled(4)
    fig, ax = plt.subplots(figsize=(8, max(5, n_wl * 0.5 + 1)))
    im = ax.imshow(mat, aspect="auto", cmap=cmap, vmin=-0.5, vmax=3.5)
    cbar = plt.colorbar(im, ax=ax, ticks=[0, 1, 2, 3])
    cbar.set_ticklabels(STRATEGIES)

    ax.set_xticks(range(n_th))
    ax.set_xticklabels([f"{r['num_threads']}TH" for r in records])
    ax.set_yticks(range(n_wl))
    ax.set_yticklabels(workloads)
    ax.set_title(f"Best Strategy per Workload × Thread Count  (label={label_by})")

    # 予測が外れたセルに × を表示
    for j, r in enumerate(records):
        for i, wl in enumerate(workloads):
            if wl in r["workloads"]:
                idx = r["workloads"].index(wl)
                if r["y_true"][idx] != r["y_pred"][idx]:
                    ax.text(j, i, "✗", ha="center", va="center",
                            color="white", fontsize=12, fontweight="bold")

    path = out_dir / f"workload_strategy_heatmap_{label_by}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVE] {path.name}")


def print_summary_table(records: list[dict], label_by: str):
    print(f"\n{'='*70}")
    print(f"  まとめ (label={label_by})")
    print(f"{'='*70}")
    print(f"  {'スレッド':>6}  {'精度':>6}  {'Packed':>7}  {'Scatter':>7}  {'HPO':>7}  {'MPO':>7}")
    print(f"  {'-'*60}")
    for r in records:
        cd = r["class_dist"]
        print(f"  {r['num_threads']:>4}TH  "
              f"{r['accuracy']:>5.1%}  "
              f"{cd.get('Packed',0):>7d}  "
              f"{cd.get('Scatter',0):>7d}  "
              f"{cd.get('HPO',0):>7d}  "
              f"{cd.get('MPO',0):>7d}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="sim_seconds",
                        choices=["sim_seconds", "energy_j"])
    args = parser.parse_args()
    label_by = args.label

    out_dir = ML_DIR / "summary"
    out_dir.mkdir(exist_ok=True)

    records = []
    for n in THREAD_NUMS:
        r = run(n, label_by)
        if r is not None:
            records.append(r)

    if not records:
        print("[ERROR] 有効な結果がありません")
        return

    print_summary_table(records, label_by)

    plot_accuracy_comparison(records, label_by, out_dir)
    plot_importance_heatmap(records, label_by, out_dir)
    plot_strategy_dist_grid(records, label_by, out_dir)
    plot_per_workload_heatmap(records, label_by, out_dir)

    # サマリー CSV
    summary_rows = []
    for r in records:
        imp = r["importances"]
        row = {"num_threads": r["num_threads"], "accuracy": r["accuracy"]}
        row.update(r["class_dist"])
        row.update({f"imp_{k}": v for k, v in imp.items()})
        summary_rows.append(row)
    pd.DataFrame(summary_rows).to_csv(out_dir / f"summary_{label_by}.csv", index=False)
    print(f"[SAVE] summary_{label_by}.csv")

    # ALLTH 統合実行
    print(f"\n[ALLTH 統合実行]")
    run_allth(label_by)

    print(f"\n結果は {out_dir} に保存されました。")


if __name__ == "__main__":
    main()
