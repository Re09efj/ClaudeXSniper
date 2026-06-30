"""
posm_baseline.py
POSM (Priority Option Switching Mechanism) ルールベースベースライン vs ML モデル比較

Jin (2023) の POSM 式に基づく戦略選択:
  M = mem_intensity  (メモリ帯域要求の代理変数)
  H = ipc_cv         (スレッド間負荷不均一性の代理変数)
  LOO fold 内で MinMax 正規化
  M_norm > H_norm → Scatter (MPO 相当)
  H_norm >= M_norm → HPO

比較手法: POSM / RF / SVM (LOO-CV)
評価指標: 精度 (accuracy) + 性能比 (sim_seconds / oracle)
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

POSM_DIR = Path(__file__).parent
ML_DIR   = POSM_DIR.parent
RF_DIR   = ML_DIR / "RandomForest"

STRATEGIES  = ["Packed", "Scatter", "HPO", "EPO"]
THREAD_NUMS = [2, 4, 8, 16]


# ── データ読み込み ─────────────────────────────────────────────────────

def load_data(thread_spec: str, label: str = "sim_seconds"):
    """
    thread_spec: "2" | "4" | "8" | "16" | "ALLTH"
    """
    if thread_spec == "ALLTH":
        subdir = RF_DIR / "ALLTH"
    else:
        subdir = RF_DIR / f"{thread_spec}TH"

    ds   = pd.read_csv(subdir / f"dataset_{label}.csv",     index_col=0)
    perf = pd.read_csv(subdir / f"performance_{label}.csv", index_col=0)

    y = ds["best_strategy"]
    X = ds.drop(columns=["best_strategy"])
    return X, y, perf


# ── POSM ──────────────────────────────────────────────────────────────

def posm_predict_loo(X: pd.DataFrame) -> list[str]:
    """
    LOO fold 内で mem_intensity と ipc_cv を MinMax 正規化し比較。
    M_norm > H_norm → Scatter, else → HPO
    """
    n      = len(X)
    y_pred = []

    for i in range(n):
        train_idx   = [j for j in range(n) if j != i]
        train_X     = X.iloc[train_idx]
        test_sample = X.iloc[i]

        mem_min = train_X["mem_intensity"].min()
        mem_max = train_X["mem_intensity"].max()
        ipc_min = train_X["ipc_cv"].min()
        ipc_max = train_X["ipc_cv"].max()

        norm_mem = (test_sample["mem_intensity"] - mem_min) / (mem_max - mem_min + 1e-12)
        norm_ipc = (test_sample["ipc_cv"]        - ipc_min) / (ipc_max - ipc_min + 1e-12)

        y_pred.append("Scatter" if norm_mem > norm_ipc else "HPO")

    return y_pred


# ── ML モデル (LOO-CV) ─────────────────────────────────────────────────

def rf_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    loo    = LeaveOneOut()
    y_pred = []
    for train_idx, test_idx in loo.split(X):
        train_y = y.iloc[train_idx]
        if train_y.nunique() < 2:
            y_pred.append(train_y.mode()[0])
            continue
        clf = RandomForestClassifier(
            n_estimators=200, random_state=42, class_weight="balanced"
        )
        clf.fit(X.iloc[train_idx], train_y)
        y_pred.append(clf.predict(X.iloc[test_idx])[0])
    return y_pred


def svm_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    loo    = LeaveOneOut()
    y_pred = []
    for train_idx, test_idx in loo.split(X):
        train_y = y.iloc[train_idx]
        if train_y.nunique() < 2:
            y_pred.append(train_y.mode()[0])
            continue
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("svm",    SVC(kernel="rbf", C=10.0, gamma="scale",
                           class_weight="balanced", random_state=42)),
        ])
        pipe.fit(X.iloc[train_idx], train_y)
        y_pred.append(pipe.predict(X.iloc[test_idx])[0])
    return y_pred


# ── 性能比計算 ────────────────────────────────────────────────────────

def perf_ratio(y_true: pd.Series, y_pred: list[str], perf: pd.DataFrame) -> float:
    """
    mean(sim_seconds[predicted] / sim_seconds[oracle])
    oracle = argmin across available strategies
    """
    avail  = [s for s in STRATEGIES if s in perf.columns]
    ratios = []
    for idx, (true_label, pred_label) in enumerate(zip(y_true.index, y_pred)):
        row    = perf.loc[true_label, avail]
        oracle = row.min()
        pred_s = pred_label if pred_label in avail else avail[0]
        ratios.append(perf.loc[true_label, pred_s] / oracle)
    return float(np.mean(ratios))


# ── 評価1スペック ─────────────────────────────────────────────────────

def evaluate(thread_spec: str, label: str = "sim_seconds") -> dict:
    X, y, perf = load_data(thread_spec, label)
    n = len(X)
    dist = y.value_counts().to_dict()

    posm_pred = posm_predict_loo(X)
    rf_pred   = rf_predict_loo(X, y)
    svm_pred  = svm_predict_loo(X, y)

    acc_posm = accuracy_score(y, posm_pred)
    acc_rf   = accuracy_score(y, rf_pred)
    acc_svm  = accuracy_score(y, svm_pred)

    r_posm = perf_ratio(y, posm_pred, perf)
    r_rf   = perf_ratio(y, rf_pred,   perf)
    r_svm  = perf_ratio(y, svm_pred,  perf)

    label_th = f"{thread_spec}TH" if thread_spec != "ALLTH" else "ALLTH"
    print(f"\n{'─'*70}")
    print(f"  {label_th}  n={n}  分布={dist}")
    print(f"  {'手法':<10} {'精度':>8}  {'性能比(x)':>10}  (性能比: 1.0=oracle と同等)")
    print(f"  {'POSM':<10} {acc_posm:>7.1%}  {r_posm:>10.4f}")
    print(f"  {'RF':<10} {acc_rf:>7.1%}  {r_rf:>10.4f}")
    print(f"  {'SVM':<10} {acc_svm:>7.1%}  {r_svm:>10.4f}")

    print(f"\n  POSM 予測詳細 ({label_th}):")
    print(f"  {'Sample':<18} {'True':<10} {'POSM':<10} {'RF':<10} {'SVM':<10}")
    for s, t, pp, rp, sp in zip(y.index, y, posm_pred, rf_pred, svm_pred):
        pok = "✓" if pp == t else "✗"
        rok = "✓" if rp == t else "✗"
        sok = "✓" if sp == t else "✗"
        print(f"  {s:<18} {t:<10} {pp+pok:<11} {rp+rok:<11} {sp+sok:<10}")

    return {
        "spec": thread_spec, "n": n,
        "acc_posm": acc_posm, "acc_rf": acc_rf, "acc_svm": acc_svm,
        "r_posm": r_posm, "r_rf": r_rf, "r_svm": r_svm,
    }


# ── プロット ──────────────────────────────────────────────────────────

def plot_comparison(records: list[dict], out_dir: Path):
    labels = [f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH" for r in records]
    x = np.arange(len(labels))
    w = 0.25

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # 精度グラフ
    ax = axes[0]
    ax.bar(x - w, [r["acc_posm"] for r in records], w, label="POSM", color="#e74c3c")
    ax.bar(x,     [r["acc_rf"]   for r in records], w, label="RF",   color="#3498db")
    ax.bar(x + w, [r["acc_svm"]  for r in records], w, label="SVM",  color="#2ecc71")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.15); ax.set_ylabel("LOO-CV Accuracy")
    ax.set_title(f"精度比較 (sim_seconds)")
    ax.axhline(0.5, color="gray", ls="--", alpha=0.5, label="random BL")
    for bar, v in zip(ax.patches, [r["acc_posm"] for r in records]
                                 + [r["acc_rf"]   for r in records]
                                 + [r["acc_svm"]  for r in records]):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.02,
                f"{bar.get_height():.0%}", ha="center", va="bottom", fontsize=8)
    ax.legend(fontsize=8)

    # 性能比グラフ
    ax = axes[1]
    ax.bar(x - w, [r["r_posm"] for r in records], w, label="POSM", color="#e74c3c")
    ax.bar(x,     [r["r_rf"]   for r in records], w, label="RF",   color="#3498db")
    ax.bar(x + w, [r["r_svm"]  for r in records], w, label="SVM",  color="#2ecc71")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("平均性能比 (sim_sec / oracle, ↓ が良)")
    ax.set_title("性能比較 (1.0 = oracle と完全一致)")
    ax.axhline(1.0, color="gray", ls="--", alpha=0.5, label="oracle")
    ax.legend(fontsize=8)

    fig.tight_layout()
    out_path = out_dir / "posm_vs_ml_comparison.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  [SAVE] {out_path}")


# ── メイン ────────────────────────────────────────────────────────────

def main():
    label   = "sim_seconds"
    out_dir = POSM_DIR
    out_dir.mkdir(exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  POSM vs RF vs SVM — LOO-CV 比較 (label={label})")
    print(f"  POSM ルール: norm(mem_intensity) > norm(ipc_cv) → Scatter, else → HPO")
    print(f"{'='*70}")

    records = []
    for th in THREAD_NUMS:
        r = evaluate(str(th), label)
        records.append(r)

    r_allth = evaluate("ALLTH", label)
    records.append(r_allth)

    # サマリーテーブル
    print(f"\n{'='*70}")
    print(f"  ▼ 精度サマリー (LOO-CV, sim_seconds)")
    print(f"  {'':>8}  {'POSM':>8}  {'RF':>8}  {'SVM':>8}")
    for r in records:
        lbl = f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH"
        print(f"  {lbl:>8}  {r['acc_posm']:>7.1%}  {r['acc_rf']:>7.1%}  {r['acc_svm']:>7.1%}")

    print(f"\n  ▼ 性能比サマリー (1.0 = oracle 選択と同等)")
    print(f"  {'':>8}  {'POSM':>8}  {'RF':>8}  {'SVM':>8}")
    for r in records:
        lbl = f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH"
        print(f"  {lbl:>8}  {r['r_posm']:>8.4f}  {r['r_rf']:>8.4f}  {r['r_svm']:>8.4f}")

    # CSV 保存
    df_out = pd.DataFrame(records)
    df_out.to_csv(out_dir / "comparison.csv", index=False)
    print(f"\n  [SAVE] comparison.csv")

    # プロット
    plot_comparison(records, out_dir)


if __name__ == "__main__":
    main()
