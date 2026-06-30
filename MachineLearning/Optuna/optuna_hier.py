"""
optuna_hier.py
階層型分類器の設計空間を Optuna で黒箱最適化する。

探索空間:
  - L1 の分割クラス: {Scatter-first, EPO-first, Packed-first}
  - L1 / L2 のモデル: {RF, SVM, LogReg}
  - 各モデルのハイパーパラメータ
  - 目的関数: LOO-CV accuracy（最大化）を主に、perf_ratio（最小化）を副次的に

また Decision Tree による自動分割構造の発見も実行し、比較する。

Usage:
  python3 optuna_hier.py [--n-trials 200] [--label sim_seconds] [--scope allth|per_thread]
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier, export_text, plot_tree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from MachineLearning.ml_utils import (
    STRATEGIES, THREAD_NUMS, OUTPUTS_DIR, FEATURE_COLS, FEATURE_COLS_ALLTH,
    collect_dataset, build_allth_data,
)

OUT_DIR   = Path(__file__).parent
RF_DIR    = Path(__file__).parent.parent / "RandomForest"
optuna.logging.set_verbosity(optuna.logging.WARNING)


# ── データ読み込み（RF が保存した CSV を再利用） ────────────────────────
def load_allth(label: str) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    ds   = pd.read_csv(RF_DIR / "ALLTH" / f"dataset_{label}.csv",     index_col=0)
    perf = pd.read_csv(RF_DIR / "ALLTH" / f"performance_{label}.csv", index_col=0)
    return ds.drop(columns=["best_strategy"]), ds["best_strategy"], perf


def load_per_thread(th: int, label: str) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    ds   = pd.read_csv(RF_DIR / f"{th}TH" / f"dataset_{label}.csv",     index_col=0)
    perf = pd.read_csv(RF_DIR / f"{th}TH" / f"performance_{label}.csv", index_col=0)
    return ds.drop(columns=["best_strategy"]), ds["best_strategy"], perf


# ── 性能比 ──────────────────────────────────────────────────────────────
def perf_ratio(y_true: pd.Series, y_pred: list[str], perf: pd.DataFrame) -> float:
    avail = [s for s in STRATEGIES if s in perf.columns]
    ratios = []
    for idx, pred in enumerate(y_pred):
        row    = perf.loc[y_true.index[idx], avail]
        oracle = row.min()
        ps     = pred if pred in avail else avail[0]
        ratios.append(perf.loc[y_true.index[idx], ps] / oracle)
    return float(np.mean(ratios))


# ── モデルビルダー ───────────────────────────────────────────────────────
def build_clf(trial: optuna.Trial, prefix: str, n_classes: int):
    """trial からモデルを構築。prefix でハイパーパラメータ名を区別。"""
    kind = trial.suggest_categorical(f"{prefix}_kind", ["rf", "svm", "lr"])

    if kind == "rf":
        n_est  = trial.suggest_int(f"{prefix}_n_est", 50, 500, step=50)
        max_d  = trial.suggest_int(f"{prefix}_max_depth", 2, 10)
        min_sl = trial.suggest_int(f"{prefix}_min_samples_leaf", 1, 4)
        return RandomForestClassifier(
            n_estimators=n_est, max_depth=max_d,
            min_samples_leaf=min_sl,
            class_weight="balanced", random_state=42,
        )
    elif kind == "svm":
        C     = trial.suggest_float(f"{prefix}_C",     1e-2, 1e2, log=True)
        gamma = trial.suggest_categorical(f"{prefix}_gamma", ["scale", "auto"])
        return Pipeline([
            ("sc",  StandardScaler()),
            ("svm", SVC(kernel="rbf", C=C, gamma=gamma,
                        class_weight="balanced", random_state=42)),
        ])
    else:  # lr
        C = trial.suggest_float(f"{prefix}_lr_C", 1e-2, 1e2, log=True)
        return Pipeline([
            ("sc", StandardScaler()),
            ("lr", LogisticRegression(C=C, class_weight="balanced",
                                      max_iter=1000, random_state=42)),
        ])


# ── 階層型 LOO-CV ─────────────────────────────────────────────────────
def hierarchical_loo(
    X: pd.DataFrame,
    y: pd.Series,
    l1_positive: str,
    trial: optuna.Trial,
) -> list[str]:
    """
    L1: l1_positive vs rest
    L2: rest の中でマルチクラス分類
    """
    n       = len(X)
    y_l1    = (y == l1_positive).map({True: l1_positive, False: "rest"})
    classes = sorted(y.unique())
    rest_classes = [c for c in classes if c != l1_positive]
    y_pred  = []

    for i in range(n):
        tr   = [j for j in range(n) if j != i]
        te_X = X.iloc[[i]]
        tr_y      = y.iloc[tr]
        tr_y_l1   = y_l1.iloc[tr]

        # L1
        if tr_y_l1.nunique() < 2:
            y_pred.append(tr_y_l1.mode()[0].replace("rest", rest_classes[0]))
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            l1_clf = build_clf(trial, "l1", 2)
            l1_clf.fit(X.iloc[tr], tr_y_l1)
            l1_pred = l1_clf.predict(te_X)[0]

        if l1_pred == l1_positive:
            y_pred.append(l1_positive)
            continue

        # L2: rest クラスのみで学習
        mask     = tr_y.isin(rest_classes)
        tr2_idx  = [tr[j] for j, m in enumerate(mask) if m]

        if not tr2_idx or y.iloc[tr2_idx].nunique() < 2:
            y_pred.append(y.iloc[tr2_idx].mode()[0] if tr2_idx else rest_classes[0])
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            l2_clf = build_clf(trial, "l2", len(rest_classes))
            l2_clf.fit(X.iloc[tr2_idx], y.iloc[tr2_idx])
            y_pred.append(l2_clf.predict(te_X)[0])

    return y_pred


# ── Optuna 目的関数 ────────────────────────────────────────────────────
def make_objective(X: pd.DataFrame, y: pd.Series, perf: pd.DataFrame):
    classes = sorted(y.unique())
    # Packed が存在しない場合のフォールバック
    candidates = [c for c in ["Scatter", "EPO", "Packed"] if c in classes]

    def objective(trial: optuna.Trial) -> float:
        l1_pos = trial.suggest_categorical("l1_positive", candidates)
        y_pred = hierarchical_loo(X, y, l1_pos, trial)
        acc    = accuracy_score(y, y_pred)
        ratio  = perf_ratio(y, y_pred, perf)
        # 精度を主目的、性能比を副次的に加算（係数 0.1 で正規化）
        return acc - 0.1 * (ratio - 1.0)

    return objective


# ── 最良設定で詳細評価 ───────────────────────────────────────────────
def evaluate_best(
    X: pd.DataFrame, y: pd.Series, perf: pd.DataFrame,
    best_params: dict, label_tag: str,
) -> dict:
    """best_params を固定した試行で LOO-CV を再実行し、詳細を返す。"""

    # Optuna の trial-like オブジェクトを模倣
    class FixedTrial:
        def __init__(self, params):
            self._params = params
        def suggest_categorical(self, name, choices):
            return self._params[name]
        def suggest_int(self, name, lo, hi, step=1):
            return self._params[name]
        def suggest_float(self, name, lo, hi, log=False):
            return self._params[name]

    ft     = FixedTrial(best_params)
    l1_pos = best_params["l1_positive"]
    y_pred = hierarchical_loo(X, y, l1_pos, ft)
    acc    = accuracy_score(y, y_pred)
    ratio  = perf_ratio(y, y_pred, perf)
    return {"y_pred": y_pred, "accuracy": acc, "perf_ratio": ratio}


# ── Decision Tree による自動構造発見 ─────────────────────────────────
def decision_tree_analysis(
    X: pd.DataFrame, y: pd.Series, perf: pd.DataFrame,
    out_dir: Path, label: str,
):
    classes = sorted(y.unique())
    print(f"\n{'='*65}")
    print(f"  Decision Tree — 自動分割構造発見 (label={label})")
    print(f"{'='*65}")

    best_acc   = 0.0
    best_depth = 2
    results    = []

    for max_d in [2, 3, 4, None]:
        loo, y_true_list, y_pred_list = LeaveOneOut(), [], []
        for tr, te in loo.split(X):
            dt = DecisionTreeClassifier(
                max_depth=max_d, class_weight="balanced", random_state=42
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                dt.fit(X.iloc[tr], y.iloc[tr])
            y_true_list.append(y.iloc[te[0]])
            y_pred_list.append(dt.predict(X.iloc[te])[0])

        acc   = accuracy_score(y_true_list, y_pred_list)
        ratio = perf_ratio(y, y_pred_list, perf)
        tag   = str(max_d) if max_d else "無制限"
        results.append((tag, acc, ratio))
        print(f"  depth={tag:<4}  accuracy={acc:.3f}  perf_ratio={ratio:.5f}")
        if acc > best_acc:
            best_acc   = acc
            best_depth = max_d

    # 最良の depth で全データ学習 → 構造を可視化
    dt_best = DecisionTreeClassifier(
        max_depth=best_depth, class_weight="balanced", random_state=42
    )
    dt_best.fit(X, y)

    print(f"\n  ── DT (depth={best_depth}) のルール ──")
    print(export_text(dt_best, feature_names=list(X.columns)))

    # 可視化
    fig, ax = plt.subplots(figsize=(max(14, 4 * (best_depth or 4)), 6))
    plot_tree(dt_best, feature_names=list(X.columns),
              class_names=classes, filled=True, rounded=True,
              impurity=False, ax=ax, fontsize=9)
    ax.set_title(f"Decision Tree (depth={best_depth}, label={label})",
                 fontsize=13, fontweight="bold")
    fig.tight_layout()
    out = out_dir / f"decision_tree_{label}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  [SAVE] {out}")

    return results, dt_best


# ── メイン ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-trials", type=int, default=200)
    ap.add_argument("--label",    default="sim_seconds")
    ap.add_argument("--scope",    default="allth", choices=["allth", "per_thread"])
    args = ap.parse_args()

    label   = args.label
    out_dir = OUT_DIR
    out_dir.mkdir(exist_ok=True)

    # ─ データ収集 ─
    scopes: list[tuple[str, pd.DataFrame, pd.Series, pd.DataFrame]] = []
    if args.scope == "allth":
        X, y, perf = load_allth(label)
        scopes.append(("ALLTH", X, y, perf))
    else:
        for th in THREAD_NUMS:
            X, y, perf = load_per_thread(th, label)
            if len(y.unique()) >= 2:   # 1 クラスのみの場合はスキップ
                scopes.append((f"{th}TH", X, y, perf))

    for scope_name, X, y, perf in scopes:
        print(f"\n{'='*65}")
        print(f"  Optuna BBO — 階層型分類設計最適化")
        print(f"  スコープ={scope_name}  n={len(X)}  trials={args.n_trials}")
        print(f"  分布: {y.value_counts().to_dict()}")
        print(f"{'='*65}")

        # ─ DT 先行分析 ─
        dt_results, dt_best = decision_tree_analysis(X, y, perf, out_dir, label)

        # ─ Optuna 最適化 ─
        print(f"\n  Optuna 探索中 ({args.n_trials} trials) ...")
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        objective = make_objective(X, y, perf)
        study.optimize(objective, n_trials=args.n_trials, show_progress_bar=False)

        bp     = study.best_params
        bval   = study.best_value
        print(f"\n  ── Optuna 最良設定 ──")
        print(f"  目的値 = {bval:.4f}")
        for k, v in bp.items():
            print(f"    {k}: {v}")

        # ─ 最良設定で詳細評価 ─
        res = evaluate_best(X, y, perf, bp, scope_name)
        print(f"\n  ── 最良設定 LOO-CV 結果 ──")
        print(f"  L1 positive class: {bp['l1_positive']}")
        print(f"  accuracy   = {res['accuracy']:.3f}  ({int(res['accuracy']*len(X))}/{len(X)})")
        print(f"  perf_ratio = {res['perf_ratio']:.5f}")
        print(f"\n  {'Sample':<20} {'True':<10} {'Pred':<10} {'OK?'}")
        print(f"  {'-'*52}")
        for name, t, p in zip(X.index, y, res["y_pred"]):
            ok = "✓" if t == p else "✗"
            print(f"  {name:<20} {t:<10} {p:<10} {ok}")

        # ─ 重要度プロット ─
        try:
            param_imp = optuna.importance.get_param_importances(study)
            fig, ax   = plt.subplots(figsize=(10, max(4, len(param_imp) * 0.35)))
            keys = list(param_imp.keys())[::-1]
            vals = [param_imp[k] for k in keys]
            ax.barh(keys, vals, color="steelblue")
            ax.set_xlabel("Importance")
            ax.set_title(f"Optuna HP Importance ({scope_name}, label={label})",
                         fontsize=12, fontweight="bold")
            fig.tight_layout()
            imp_out = out_dir / f"param_importance_{scope_name}_{label}.png"
            fig.savefig(imp_out, dpi=150, bbox_inches="tight")
            plt.close(fig)
            print(f"\n  [SAVE] {imp_out}")
        except Exception:
            pass

        # ─ 最適化履歴プロット ─
        fig, ax = plt.subplots(figsize=(10, 4))
        vals_hist = [t.value for t in study.trials if t.value is not None]
        ax.plot(vals_hist, alpha=0.4, color="steelblue", label="trial")
        ax.plot(np.maximum.accumulate(vals_hist), color="red", lw=2, label="best")
        ax.set_xlabel("Trial")
        ax.set_ylabel("Objective (acc - 0.1*(ratio-1))")
        ax.set_title(f"Optuna Optimization History ({scope_name})")
        ax.legend()
        fig.tight_layout()
        hist_out = out_dir / f"optim_history_{scope_name}_{label}.png"
        fig.savefig(hist_out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  [SAVE] {hist_out}")

        # ─ 全手法精度サマリー（DT と Optuna 最良を並べる） ─
        print(f"\n  ── 精度サマリー ({scope_name}) ──")
        print(f"  {'手法':<20} {'accuracy':>9}  {'perf_ratio':>11}")
        print(f"  {'-'*45}")
        for tag, acc, ratio in dt_results:
            print(f"  {'DT(depth='+tag+')':<20} {acc:>9.3f}  {ratio:>11.5f}")
        print(f"  {'Optuna-Hier':<20} {res['accuracy']:>9.3f}  {res['perf_ratio']:>11.5f}")

        # ─ CSV 保存 ─
        rows = [{"method": f"DT(depth={t})", "accuracy": a, "perf_ratio": r}
                for t, a, r in dt_results]
        rows.append({"method": "Optuna-Hier",
                     "accuracy": res["accuracy"], "perf_ratio": res["perf_ratio"]})
        rows.append({"method": "best_params", **bp})
        pd.DataFrame(rows).to_csv(
            out_dir / f"results_{scope_name}_{label}.csv", index=False)
        print(f"  [SAVE] results_{scope_name}_{label}.csv")


if __name__ == "__main__":
    main()
