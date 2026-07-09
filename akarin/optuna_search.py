"""
optuna_search.py
AKARINシステムのalpha探索。

akarin/cpsat_mapper.py の目的関数 alpha*makespan + (1-alpha)*remote_penalty の
alpha は理論的に「正解」を決められないパラメータであり、実際にSniperで走らせた
結果（sim_time）でしか良し悪しが分からない。そこで Optuna のベイズ最適化(TPE)で
少ない試行回数(既定15回)から良い alpha を探索する。

1試行 = CP-SATでcpu_map計算(高速) → そのcpu_mapで実際にSniperをフル実行(低速、
ここがボトルネック) → sim_time_ms を測定 → Optunaにフィードバック、という流れ。
Sniperは同一cpu_mapに対して決定論的なので、同じ候補を再評価する必要はない。

出力は本体の Outputs/sizeX/ 以下とは分離し、Outputs/akarin/ 配下にまとめる
（既存のPacked/Scatter/HPO/EPO/MPO比較用データと混ざらないように）。

使い方:
  python3 -m akarin.optuna_search --workload BT --bench-class S --threads 2 --trials 10
"""

import argparse
import os
from datetime import datetime

import optuna

from akarin.cpsat_mapper import compute_cpsat_map_from_csv
from config.generate_config import generate_config, get_config_path
from utility.cpu_affinity import binary_path, get_binary_args, save_affinity_config
from utility.deloc_mapper import find_comm_csv
from utility.stats_reader import parse_sim_time
from utility.sniper_sim_sid import run_sniper

OUTPUT_BASE = "/home/hiragahama/ClaudeXSniper/Outputs/akarin"
STUDY_DB = "/home/hiragahama/ClaudeXSniper/akarin/optuna_studies.db"


def run_trial(
    workload: str,
    bench_class: str,
    num_threads: int,
    alpha: float,
    run_id: str,
    trial_number: int,
) -> tuple[float | None, str, dict]:
    """
    指定alphaでCP-SAT解を計算し、実際にSniperを実行してsim_secondsを返す。
    失敗時は (None, out_dir, info) を返す。
    """
    csv_path = find_comm_csv(workload, bench_class, num_threads)
    cpu_map, cpsat_info, imbalance = compute_cpsat_map_from_csv(csv_path, num_threads, alpha=alpha)

    strategy_label = f"AKARIN_a{alpha:.3f}"
    out_dir = os.path.join(
        OUTPUT_BASE, f"size{bench_class}", f"{num_threads}TH",
        f"{workload}_{bench_class}_trial{trial_number:03d}_{num_threads}TH_{run_id}",
    )
    os.makedirs(out_dir, exist_ok=True)

    bin_path = binary_path(workload, bench_class)
    bin_args = get_binary_args(workload, bench_class, num_threads)
    cfg_path = get_config_path(out_dir, strategy_label, num_threads)
    generate_config(strategy_label, num_threads, cpu_map, cfg_path)
    save_affinity_config(out_dir, strategy_label, workload, bench_class, cpu_map, num_threads)

    log_path = os.path.join(out_dir, "sniper.log")
    with open(log_path, "w") as log_file:
        ret = run_sniper(
            binary_path=bin_path,
            binary_args=bin_args,
            num_threads=num_threads,
            cpu_map=cpu_map,
            strategy=strategy_label,
            output_dir=out_dir,
            config_path=cfg_path,
            log_file=log_file,
            workload=workload,
        )

    if ret != 0:
        return None, out_dir, cpsat_info

    sim_seconds = parse_sim_time(out_dir)
    return sim_seconds, out_dir, cpsat_info


def make_objective(workload: str, bench_class: str, num_threads: int, run_id: str):
    def objective(trial: optuna.Trial) -> float:
        alpha = trial.suggest_float("alpha", 0.0, 1.0)
        sim_seconds, out_dir, cpsat_info = run_trial(
            workload, bench_class, num_threads, alpha, run_id, trial.number
        )
        trial.set_user_attr("out_dir", out_dir)
        trial.set_user_attr("cpsat_status", cpsat_info["status"])
        if sim_seconds is None:
            raise optuna.TrialPruned()
        return sim_seconds

    return objective


def search(
    workload: str,
    bench_class: str,
    num_threads: int,
    n_trials: int = 15,
    seed: int = 42,
) -> optuna.Study:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    study_name = f"{workload}_{bench_class}_{num_threads}TH_{run_id}"
    study = optuna.create_study(
        study_name=study_name,
        storage=f"sqlite:///{STUDY_DB}",
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(make_objective(workload, bench_class, num_threads, run_id), n_trials=n_trials)
    return study


def _parse_args():
    p = argparse.ArgumentParser(description="AKARIN: CP-SATのalphaをOptunaで探索する")
    p.add_argument("--workload", required=True)
    p.add_argument("--bench-class", default="S")
    p.add_argument("--threads", type=int, required=True)
    p.add_argument("--trials", type=int, default=15)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def main():
    args = _parse_args()
    study = search(args.workload, args.bench_class, args.threads, n_trials=args.trials, seed=args.seed)

    print(f"\n=== {args.workload} class={args.bench_class} {args.threads}TH ===")
    print(f"最良alpha: {study.best_params['alpha']:.4f}")
    print(f"最良sim_seconds: {study.best_value:.6f}")
    print(f"試行回数: {len(study.trials)}")
    for t in study.trials:
        status = "pruned" if t.state == optuna.trial.TrialState.PRUNED else f"{t.value:.6f}s"
        print(f"  trial{t.number:03d} alpha={t.params.get('alpha', float('nan')):.4f} -> {status}")


if __name__ == "__main__":
    main()
