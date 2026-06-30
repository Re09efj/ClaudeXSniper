"""
hierarchical_model.py
階層型戦略選択モデル (ALLTH のみ)

L1: Packed vs non-Packed  (全サンプルで学習)
L2: Scatter / HPO / MPO   (non-Packed サンプルのみで学習)

LOO-CV 手順:
  fold i を除いた 51 サンプルで L1 を学習
  そのうち non-Packed サンプルのみで L2 を学習
  → L1(x) == non-Packed なら L2(x) を、Packed なら Packed を返す

比較: POSM / RF分類 / SVM / RF回帰 / RF階層 (本モデル)
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

HIER_DIR   = Path(__file__).parent
ML_DIR     = HIER_DIR.parent
RF_DIR     = ML_DIR / "RandomForest"

STRATEGIES     = ["Packed", "Scatter", "HPO", "EPO"]
NON_PACKED     = ["Scatter", "HPO", "EPO"]
LABEL          = "sim_seconds"
ALLTH_SUBDIR   = RF_DIR / "ALLTH"


# ── データ読み込み ─────────────────────────────────────────────────────

def load_allth():
    ds   = pd.read_csv(ALLTH_SUBDIR / f"dataset_{LABEL}.csv",     index_col=0)
    perf = pd.read_csv(ALLTH_SUBDIR / f"performance_{LABEL}.csv", index_col=0)
    y    = ds["best_strategy"]
    X    = ds.drop(columns=["best_strategy"])
    return X, y, perf


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


# ── 各モデルの LOO-CV 予測 ────────────────────────────────────────────

def posm_predict_loo(X: pd.DataFrame) -> list[str]:
    n, y_pred = len(X), []
    for i in range(n):
        tr  = [j for j in range(n) if j != i]
        tx  = X.iloc[i]
        trX = X.iloc[tr]
        nm  = (tx["mem_intensity"] - trX["mem_intensity"].min()) / (trX["mem_intensity"].max() - trX["mem_intensity"].min() + 1e-12)
        nh  = (tx["ipc_cv"]        - trX["ipc_cv"].min())        / (trX["ipc_cv"].max()        - trX["ipc_cv"].min()        + 1e-12)
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
    avail  = [s for s in STRATEGIES if s in perf.columns]
    n      = len(X)
    y_pred = []
    for i in range(n):
        tr   = [j for j in range(n) if j != i]
        preds = {}
        for s in avail:
            reg = RandomForestRegressor(n_estimators=200, random_state=42)
            reg.fit(X.iloc[tr], perf.iloc[tr][s])
            preds[s] = reg.predict(X.iloc[[i]])[0]
        y_pred.append(min(preds, key=preds.get))
    return y_pred


def _make_svm_pipe(class_weight="balanced"):
    return Pipeline([("sc", StandardScaler()),
                     ("svm", SVC(kernel="rbf", C=10.0, gamma="scale",
                                 class_weight=class_weight, random_state=42))])


def hierarchical_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    """
    LOO-CV で階層型予測 (RF版)。
    L1: Packed vs non-Packed
    L2: Scatter / HPO / MPO (non-Packed サンプルのみで訓練)
    """
    n      = len(X)
    y_pred = []
    y_l1   = (y == "Packed").map({True: "Packed", False: "non-Packed"})

    for i in range(n):
        tr    = [j for j in range(n) if j != i]
        tr_y  = y.iloc[tr]
        tr_l1 = y_l1.iloc[tr]
        te_X  = X.iloc[[i]]

        # L1: Packed vs non-Packed
        l1 = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
        l1.fit(X.iloc[tr], tr_l1)
        l1_pred = l1.predict(te_X)[0]

        if l1_pred == "Packed":
            y_pred.append("Packed")
            continue

        # L2: Scatter / HPO / MPO (non-Packed のみで学習)
        mask    = tr_y.isin(NON_PACKED)
        tr2_idx = [tr[j] for j, m in enumerate(mask) if m]

        if len(tr2_idx) < 2 or y.iloc[tr2_idx].nunique() < 2:
            y_pred.append(y.iloc[tr2_idx].mode()[0] if tr2_idx else "HPO")
            continue

        l2 = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
        l2.fit(X.iloc[tr2_idx], y.iloc[tr2_idx])
        y_pred.append(l2.predict(te_X)[0])

    return y_pred


def svm_hierarchical_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    """
    LOO-CV で階層型予測 (SVM版)。
    L1: SVM (Packed vs non-Packed)
    L2: SVM (Scatter / HPO / MPO, non-Packed サンプルのみで訓練)
    """
    n      = len(X)
    y_pred = []
    y_l1   = (y == "Packed").map({True: "Packed", False: "non-Packed"})

    for i in range(n):
        tr    = [j for j in range(n) if j != i]
        tr_y  = y.iloc[tr]
        tr_l1 = y_l1.iloc[tr]
        te_X  = X.iloc[[i]]

        # L1: SVM — Packed vs non-Packed
        l1 = _make_svm_pipe()
        l1.fit(X.iloc[tr], tr_l1)
        l1_pred = l1.predict(te_X)[0]

        if l1_pred == "Packed":
            y_pred.append("Packed")
            continue

        # L2: SVM — Scatter / HPO / MPO (non-Packed のみで学習)
        mask    = tr_y.isin(NON_PACKED)
        tr2_idx = [tr[j] for j, m in enumerate(mask) if m]

        if len(tr2_idx) < 2 or y.iloc[tr2_idx].nunique() < 2:
            y_pred.append(y.iloc[tr2_idx].mode()[0] if tr2_idx else "HPO")
            continue

        l2 = _make_svm_pipe()
        l2.fit(X.iloc[tr2_idx], y.iloc[tr2_idx])
        y_pred.append(l2.predict(te_X)[0])

    return y_pred


def hybrid_hierarchical_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str]:
    """
    LOO-CV で階層型予測 (ハイブリッド版)。
    L1: RF  (Packed vs non-Packed) ← RF の方が L1 精度が高い
    L2: SVM (Scatter / HPO / MPO)  ← SVM の方が L2 精度が高い
    """
    n      = len(X)
    y_pred = []
    y_l1   = (y == "Packed").map({True: "Packed", False: "non-Packed"})

    for i in range(n):
        tr    = [j for j in range(n) if j != i]
        tr_y  = y.iloc[tr]
        tr_l1 = y_l1.iloc[tr]
        te_X  = X.iloc[[i]]

        # L1: RF — Packed vs non-Packed
        l1 = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")
        l1.fit(X.iloc[tr], tr_l1)
        l1_pred = l1.predict(te_X)[0]

        if l1_pred == "Packed":
            y_pred.append("Packed")
            continue

        # L2: SVM — Scatter / HPO / MPO (non-Packed のみで学習)
        mask    = tr_y.isin(NON_PACKED)
        tr2_idx = [tr[j] for j, m in enumerate(mask) if m]

        if len(tr2_idx) < 2 or y.iloc[tr2_idx].nunique() < 2:
            y_pred.append(y.iloc[tr2_idx].mode()[0] if tr2_idx else "HPO")
            continue

        l2 = _make_svm_pipe()
        l2.fit(X.iloc[tr2_idx], y.iloc[tr2_idx])
        y_pred.append(l2.predict(te_X)[0])

    return y_pred


# ── 予測詳細の表示 ────────────────────────────────────────────────────

def print_detail(y: pd.Series, preds: dict[str, list[str]]):
    methods = list(preds.keys())
    header  = f"  {'Sample':<18} {'True':<10}" + "".join(f" {m:<12}" for m in methods)
    print(header)
    for i, (sample, true) in enumerate(zip(y.index, y)):
        row = f"  {sample:<18} {true:<10}"
        for m in methods:
            p   = preds[m][i]
            ok  = "✓" if p == true else "✗"
            row += f" {(p+ok):<12}"
        print(row)


# ── プロット ──────────────────────────────────────────────────────────

def plot_results(names: list[str], accs: list[float], ratios: list[float],
                 y: pd.Series, hier_pred: list[str], svm_hier_pred: list[str],
                 hybrid_pred: list[str]):
    colors = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#8e44ad", "#1abc9c", "#e67e22"]
    fig, axes = plt.subplots(1, 5, figsize=(28, 5))

    # 精度バー
    ax = axes[0]
    bars = ax.bar(names, accs, color=colors[:len(names)])
    ax.set_ylim(0, 1.15); ax.set_ylabel("LOO-CV Accuracy")
    ax.set_title("Accuracy (ALLTH)")
    ax.axhline(0.5, color="gray", ls="--", alpha=0.4)
    for bar, v in zip(bars, accs):
        ax.text(bar.get_x() + bar.get_width()/2, v + 0.02,
                f"{v:.1%}", ha="center", va="bottom", fontsize=9)

    # 性能比バー
    ax = axes[1]
    bars = ax.bar(names, ratios, color=colors[:len(names)])
    ax.set_ylabel("Perf Ratio (sim_sec / oracle)")
    ax.set_title("Performance Ratio (ALLTH, lower is better)")
    ax.axhline(1.0, color="gray", ls="--", alpha=0.4)
    for bar, v in zip(bars, ratios):
        ax.text(bar.get_x() + bar.get_width()/2, v + 0.001,
                f"{v:.4f}", ha="center", va="bottom", fontsize=8)

    # 混同行列: RF階層 / SVM階層 / ハイブリッド を並べて表示
    for ax, pred_list, title in [
        (axes[2], hier_pred,     "RF Hier CM"),
        (axes[3], svm_hier_pred, "SVM Hier CM"),
        (axes[4], hybrid_pred,   "Hybrid Hier CM"),
    ]:
        labs = [s for s in STRATEGIES if s in y.unique() or s in pred_list]
        cm   = confusion_matrix(y, pred_list, labels=labs)
        im   = ax.imshow(cm, cmap=plt.cm.Purples)
        plt.colorbar(im, ax=ax)
        ax.set_xticks(range(len(labs))); ax.set_xticklabels(labs, rotation=45, ha="right")
        ax.set_yticks(range(len(labs))); ax.set_yticklabels(labs)
        ax.set_xlabel("Predicted"); ax.set_ylabel("True")
        ax.set_title(f"{title} (ALLTH)")
        for ii in range(len(labs)):
            for jj in range(len(labs)):
                ax.text(jj, ii, str(cm[ii, jj]), ha="center", va="center",
                        color="white" if cm[ii, jj] > cm.max()/2 else "black")

    fig.tight_layout()
    out = HIER_DIR / "hierarchical_comparison_allth.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  [SAVE] {out}")


# ── メイン ────────────────────────────────────────────────────────────

def main():
    X, y, perf = load_allth()
    n = len(X)
    dist = y.value_counts().to_dict()

    print(f"\n{'='*70}")
    print(f"  階層型モデル vs 全手法比較  (ALLTH, label={LABEL})")
    print(f"  n={n}  分布={dist}")
    print(f"  non-Packed サンプル数={sum(v for k, v in dist.items() if k != 'Packed')}")
    print(f"{'='*70}")
    print("  計算中 ...", flush=True)

    posm_pred     = posm_predict_loo(X)
    rf_pred       = rf_clf_predict_loo(X, y)
    svm_pred      = svm_predict_loo(X, y)
    rf_reg        = rf_reg_predict_loo(X, perf)
    hier_pred     = hierarchical_predict_loo(X, y)
    svm_hier_pred = svm_hierarchical_predict_loo(X, y)
    hybrid_pred   = hybrid_hierarchical_predict_loo(X, y)

    all_preds = {
        "POSM":    posm_pred,
        "RF分類":  rf_pred,
        "SVM":     svm_pred,
        "RF回帰":  rf_reg,
        "RF階層":  hier_pred,
        "SVM階層": svm_hier_pred,
        "RF+SVM階層": hybrid_pred,
    }

    names  = list(all_preds.keys())
    accs   = [accuracy_score(y, p)  for p in all_preds.values()]
    ratios = [perf_ratio(y, p, perf) for p in all_preds.values()]

    print(f"\n  {'手法':<10} {'精度':>8}  {'性能比':>10}")
    print(f"  {'-'*35}")
    for name, acc, ratio in zip(names, accs, ratios):
        best_acc   = " ◀精度最良"   if acc   == max(accs)   else ""
        best_ratio = " ◀性能比最良" if ratio == min(ratios) else ""
        print(f"  {name:<10} {acc:>7.1%}  {ratio:>10.4f}{best_acc}{best_ratio}")

    print(f"\n  予測詳細:")
    print_detail(y, all_preds)

    # L1/L2 の分析
    y_l1 = (y == "Packed").map({True: "Packed", False: "non-Packed"})
    non_packed_mask = y.isin(NON_PACKED)

    for label_name, pred_list in [("RF階層", hier_pred), ("SVM階層", svm_hier_pred), ("RF+SVM階層", hybrid_pred)]:
        print(f"\n  ── {label_name} L1/L2 分析 ──")
        l1_tags = ["Packed" if p == "Packed" else "non-Packed" for p in pred_list]
        print(f"  L1 精度 (Packed vs non-Packed): {accuracy_score(y_l1, l1_tags):.1%}")
        if non_packed_mask.any():
            y_np    = y[non_packed_mask]
            pred_np = [p for p, m in zip(pred_list, non_packed_mask) if m]
            print(f"  L2 精度 (non-Packed サンプルのみ): {accuracy_score(y_np, pred_np):.1%}  (n={non_packed_mask.sum()})")
            print(f"  L2 分布 (真): {y_np.value_counts().to_dict()}")
            print(f"  L2 予測分布:  {pd.Series(pred_np).value_counts().to_dict()}")

    # CSV 保存
    df = pd.DataFrame({"method": names, "accuracy": accs, "perf_ratio": ratios})
    df.to_csv(HIER_DIR / "comparison_allth.csv", index=False)
    print(f"\n  [SAVE] comparison_allth.csv")

    plot_results(names, accs, ratios, y, hier_pred, svm_hier_pred, hybrid_pred)


if __name__ == "__main__":
    main()
