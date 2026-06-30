"""
regression_model.py
回帰ベース戦略選択モデル

各戦略の sim_seconds を個別に回帰予測し、予測値が最小の戦略を選択する。
  → 学習目標が「性能比最小化」と直接一致する

比較対象: POSM / RF分類 / SVM分類 / RF回帰 (本モデル)
評価指標: 精度 (accuracy) + 性能比 (sim_seconds / oracle)
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import accuracy_score
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

REG_DIR    = Path(__file__).parent
ML_DIR     = REG_DIR.parent
RF_DIR     = ML_DIR / "RandomForest"

STRATEGIES  = ["Packed", "Scatter", "HPO", "EPO"]
THREAD_NUMS = [2, 4, 8, 16]


# ── データ読み込み ─────────────────────────────────────────────────────

def load_data(thread_spec: str, label: str = "sim_seconds"):
    subdir = RF_DIR / ("ALLTH" if thread_spec == "ALLTH" else f"{thread_spec}TH")
    ds   = pd.read_csv(subdir / f"dataset_{label}.csv",     index_col=0)
    perf = pd.read_csv(subdir / f"performance_{label}.csv", index_col=0)
    y = ds["best_strategy"]
    X = ds.drop(columns=["best_strategy"])
    return X, y, perf


# ── 各モデルの LOO-CV 予測 ────────────────────────────────────────────

def posm_predict_loo(X: pd.DataFrame) -> list[str]:
    """norm(mem_intensity) > norm(ipc_cv) → Scatter, else → HPO"""
    n, y_pred = len(X), []
    for i in range(n):
        tr = [j for j in range(n) if j != i]
        tx = X.iloc[i]
        trX = X.iloc[tr]
        nm = (tx["mem_intensity"] - trX["mem_intensity"].min()) / (trX["mem_intensity"].max() - trX["mem_intensity"].min() + 1e-12)
        nh = (tx["ipc_cv"]        - trX["ipc_cv"].min())        / (trX["ipc_cv"].max()        - trX["ipc_cv"].min()        + 1e-12)
        y_pred.append("Scatter" if nm > nh else "HPO")
    return y_pred


def rf_clf_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    loo, y_pred = LeaveOneOut(), []
    for tr, te in loo.split(X):
        ty = y.iloc[tr]
        if ty.nunique() < 2:
            y_pred.append(ty.mode()[0]); continue
        clf = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
        clf.fit(X.iloc[tr], ty)
        y_pred.append(clf.predict(X.iloc[te])[0])
    return y_pred


def svm_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    loo, y_pred = LeaveOneOut(), []
    for tr, te in loo.split(X):
        ty = y.iloc[tr]
        if ty.nunique() < 2:
            y_pred.append(ty.mode()[0]); continue
        pipe = Pipeline([("sc", StandardScaler()),
                         ("svm", SVC(kernel="rbf", C=10.0, gamma="scale",
                                     class_weight="balanced", random_state=42))])
        pipe.fit(X.iloc[tr], ty)
        y_pred.append(pipe.predict(X.iloc[te])[0])
    return y_pred


def rf_reg_predict_loo(X: pd.DataFrame, perf: pd.DataFrame) -> list[str]:
    """
    各戦略の sim_seconds を RandomForestRegressor で個別予測。
    予測 sim_seconds が最小の戦略を選択。
    """
    avail  = [s for s in STRATEGIES if s in perf.columns]
    n      = len(X)
    y_pred = []

    for i in range(n):
        tr   = [j for j in range(n) if j != i]
        trX  = X.iloc[tr]
        teX  = X.iloc[[i]]
        preds = {}
        for s in avail:
            reg = RandomForestRegressor(n_estimators=200, random_state=42)
            reg.fit(trX, perf.iloc[tr][s])
            preds[s] = reg.predict(teX)[0]
        y_pred.append(min(preds, key=preds.get))

    return y_pred


# ── 性能比 ────────────────────────────────────────────────────────────

def perf_ratio(y_true: pd.Series, y_pred: list[str], perf: pd.DataFrame) -> float:
    avail  = [s for s in STRATEGIES if s in perf.columns]
    ratios = []
    for idx, pred in enumerate(y_pred):
        row    = perf.loc[y_true.index[idx], avail]
        oracle = row.min()
        ps     = pred if pred in avail else avail[0]
        ratios.append(perf.loc[y_true.index[idx], ps] / oracle)
    return float(np.mean(ratios))


# ── 1スペック評価 ─────────────────────────────────────────────────────

def evaluate(thread_spec: str, label: str = "sim_seconds") -> dict:
    X, y, perf = load_data(thread_spec, label)
    n    = len(X)
    dist = y.value_counts().to_dict()
    lbl  = f"{thread_spec}TH" if thread_spec != "ALLTH" else "ALLTH"

    print(f"\n{'─'*70}")
    print(f"  {lbl}  n={n}  分布={dist}")
    print(f"  計算中 ...", flush=True)

    posm_pred   = posm_predict_loo(X)
    rf_clf_pred = rf_clf_predict_loo(X, y)
    svm_pred    = svm_predict_loo(X, y)
    rf_reg_pred = rf_reg_predict_loo(X, perf)

    results = {
        "POSM":   (posm_pred,   accuracy_score(y, posm_pred),   perf_ratio(y, posm_pred,   perf)),
        "RF分類": (rf_clf_pred, accuracy_score(y, rf_clf_pred), perf_ratio(y, rf_clf_pred, perf)),
        "SVM":    (svm_pred,    accuracy_score(y, svm_pred),    perf_ratio(y, svm_pred,    perf)),
        "RF回帰": (rf_reg_pred, accuracy_score(y, rf_reg_pred), perf_ratio(y, rf_reg_pred, perf)),
    }

    print(f"  {'手法':<10} {'精度':>8}  {'性能比(x)':>10}")
    for name, (_, acc, ratio) in results.items():
        best_acc   = "◀" if acc   == max(r[1] for r in results.values()) else ""
        best_ratio = "◀" if ratio == min(r[2] for r in results.values()) else ""
        print(f"  {name:<10} {acc:>7.1%}  {ratio:>10.4f}  {best_acc}{best_ratio}")

    print(f"\n  RF回帰 予測詳細 ({lbl}):")
    print(f"  {'Sample':<18} {'True':<10} {'POSM':<10} {'RF分類':<10} {'RF回帰':<10}")
    for s, t, pp, rp, gp in zip(y.index, y, posm_pred, rf_clf_pred, rf_reg_pred):
        pok = "✓" if pp == t else "✗"
        rok = "✓" if rp == t else "✗"
        gok = "✓" if gp == t else "✗"
        print(f"  {s:<18} {t:<10} {pp+pok:<11} {rp+rok:<11} {gp+gok:<10}")

    return {
        "spec": thread_spec, "n": n,
        **{f"acc_{k}":   v[1] for k, v in results.items()},
        **{f"ratio_{k}": v[2] for k, v in results.items()},
        "pred_rf_reg": rf_reg_pred,
        "y_true": list(y),
    }


# ── プロット ──────────────────────────────────────────────────────────

def plot_comparison(records: list[dict]):
    labels  = [f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH" for r in records]
    methods = ["POSM", "RF分類", "SVM", "RF回帰"]
    colors  = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12"]
    x = np.arange(len(labels))
    w = 0.2

    fig, axes = plt.subplots(1, 2, figsize=(15, 5))

    for ax, key, ylabel, title in [
        (axes[0], "acc",   "LOO-CV Accuracy",       "Accuracy Comparison"),
        (axes[1], "ratio", "Perf Ratio (↓ better)", "Performance Ratio (sim_sec / oracle)"),
    ]:
        for i, (m, c) in enumerate(zip(methods, colors)):
            vals = [r[f"{key}_{m}"] for r in records]
            bars = ax.bar(x + (i - 1.5) * w, vals, w, label=m, color=c)
            for bar in bars:
                h = bar.get_height()
                ax.text(bar.get_x() + bar.get_width()/2,
                        h + (0.01 if key == "acc" else 0.001),
                        f"{h:.1%}" if key == "acc" else f"{h:.3f}",
                        ha="center", va="bottom", fontsize=7)
        ax.set_xticks(x); ax.set_xticklabels(labels)
        ax.set_ylabel(ylabel); ax.set_title(title)
        if key == "acc":
            ax.set_ylim(0, 1.25); ax.axhline(0.5, color="gray", ls="--", alpha=0.4)
        else:
            ax.axhline(1.0, color="gray", ls="--", alpha=0.4, label="oracle")
        ax.legend(fontsize=8)

    fig.tight_layout()
    out = REG_DIR / "comparison_all_models.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  [SAVE] {out}")


# ── メイン ────────────────────────────────────────────────────────────

def main():
    label = "sim_seconds"

    print(f"\n{'='*70}")
    print(f"  POSM / RF分類 / SVM / RF回帰  LOO-CV 比較 (label={label})")
    print(f"  RF回帰: 各戦略の sim_seconds を予測 → argmin を選択")
    print(f"{'='*70}")

    records = []
    for th in THREAD_NUMS:
        records.append(evaluate(str(th), label))
    records.append(evaluate("ALLTH", label))

    print(f"\n{'='*70}")
    print(f"  ▼ 精度サマリー")
    print(f"  {'':>8}  {'POSM':>8}  {'RF分類':>8}  {'SVM':>8}  {'RF回帰':>8}")
    for r in records:
        lbl = f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH"
        print(f"  {lbl:>8}  {r['acc_POSM']:>7.1%}  {r['acc_RF分類']:>7.1%}  {r['acc_SVM']:>7.1%}  {r['acc_RF回帰']:>7.1%}")

    print(f"\n  ▼ 性能比サマリー (1.0 = oracle)")
    print(f"  {'':>8}  {'POSM':>8}  {'RF分類':>8}  {'SVM':>8}  {'RF回帰':>8}")
    for r in records:
        lbl = f"{r['spec']}TH" if r["spec"] != "ALLTH" else "ALLTH"
        print(f"  {lbl:>8}  {r['ratio_POSM']:>8.4f}  {r['ratio_RF分類']:>8.4f}  {r['ratio_SVM']:>8.4f}  {r['ratio_RF回帰']:>8.4f}")

    pd.DataFrame([{k: v for k, v in r.items()
                   if k not in ("pred_rf_reg", "y_true")}
                  for r in records]).to_csv(REG_DIR / "comparison.csv", index=False)
    print(f"  [SAVE] comparison.csv")

    plot_comparison(records)


if __name__ == "__main__":
    main()
