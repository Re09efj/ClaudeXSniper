"""
cpsat_mapper.py
DeLoc/MPO の「Step1(ノード決定)→Step2(ノード内Big/Small)」という順序固定の
2段階ヒューリスティック（utility/deloc_mapper.py）を、OR-Tools CP-SAT による
単一の同時最適化に置き換えたもの。

背景:
  Jin の MPO はノード所属を先に固定してしまうため、重いスレッドが同じノードに
  偏るとそのノードの Big core 枠を超えた分が強制的に Small core に落ちる
  （論文3.3.1節が明記する構造的欠陥）。また「ノード内の合計負荷を均等にする」
  という代理指標（例: METISの標準的な分割目的）は、重いスレッドの"数"が
  Big core 枠を超えないことを保証しない。
  そこで、局所性（通信量）とヘテロ性（負荷×コア速度）を1つの目的関数に
  まとめ、CP-SAT に厳密解を求めさせる。

定式化:
  変数: x[t][c] ∈ {0,1}  … スレッドtをコアcに割り当てるか
  制約: 各スレッドは必ず1コア、各コアは高々1スレッド
  目的: alpha * makespan + (1-alpha) * remote_comm_penalty  を最小化
    - makespan   = max_c( thread_loads[t] / core_speed[c] )   (各コアの完了時間の最大値)
    - remote_comm_penalty = Σ_{(t1,t2)} comm[t1,t2] × (t1,t2が別ノードなら1)

alpha は外側のループ（Optuna等）で調整する想定のパラメータ。alpha=1.0 で
純粋なヘテロ性優先（HPO寄り）、alpha=0.0 で純粋な局所性優先（DeLoc寄り）になる。

単体実行:
  python3 -m utility.cpsat_mapper Data/comm_matrices/lavaMD_A_12TH_lavaMD.12.6.comm.csv --threads 12 --alpha 0.5
"""

import argparse

from ortools.sat.python import cp_model

from utility.deloc_mapper import (
    NUM_NODES,
    CORES_PER_NODE,
    NODE_P_CORES,
    NODE_E_CORES,
    load_comm_matrix,
    _pairs_from_matrix,
    load_mem_access,
    mem_access_path_for,
    compute_load_imbalance,
)

P_FREQ = 4.0   # GHz (config/generate_config.py と一致させる)
E_FREQ = 1.0   # GHz

# スケーリング係数（CP-SATは整数係数が必要なため）
_SCALE = 1000


def _core_speed(cpu_id: int) -> float:
    node = 0 if cpu_id < CORES_PER_NODE else 1
    return P_FREQ if cpu_id in NODE_P_CORES[node] else E_FREQ


def _node_of(cpu_id: int) -> int:
    return 0 if cpu_id < CORES_PER_NODE else 1


def compute_cpsat_map(
    comm_matrix: list[list[float]],
    num_threads: int,
    thread_loads: dict[int, float] | None = None,
    alpha: float = 0.5,
    time_limit_sec: float = 10.0,
) -> tuple[list[int], dict]:
    """
    CP-SAT で makespan と通信局所性を同時最適化した cpu_map を返す。
    thread_loads が None の場合は全スレッド負荷=1（純粋な局所性最適化、alpha無視）。

    戻り値: (cpu_map, info) — info には目的関数値・makespan・remote_penaltyを含む。
    """
    mat = comm_matrix[:num_threads]
    mat = [row[:num_threads] for row in mat]
    pairs = _pairs_from_matrix(mat)

    if thread_loads is None:
        thread_loads = {t: 1.0 for t in range(num_threads)}

    cores = list(range(min(CORES_PER_NODE * NUM_NODES, 16)))
    # 使用する物理コアは先頭 num_threads 個のノード内埋め順（P→E）に対応する集合とする。
    # 割当自体はモデルに解かせるが、コア"候補"はノードごとの P/E 一覧から組み立てる。
    node_cores = {0: NODE_P_CORES[0] + NODE_E_CORES[0], 1: NODE_P_CORES[1] + NODE_E_CORES[1]}
    all_cores = node_cores[0] + node_cores[1]

    model = cp_model.CpModel()

    # x[t][c]
    x = {}
    for t in range(num_threads):
        for c in all_cores:
            x[t, c] = model.NewBoolVar(f"x_{t}_{c}")

    for t in range(num_threads):
        model.Add(sum(x[t, c] for c in all_cores) == 1)
    for c in all_cores:
        model.Add(sum(x[t, c] for t in range(num_threads)) <= 1)

    # --- makespan項 ---
    max_load = max(thread_loads.values()) if thread_loads else 1.0
    finish_vars = []
    for c in all_cores:
        speed = _core_speed(c)
        # contribution[t] = load[t] / speed  (整数へスケーリング)
        finish = model.NewIntVar(0, int(max_load / min(P_FREQ, E_FREQ) * _SCALE) + 1, f"finish_{c}")
        model.Add(
            finish
            == sum(
                int(thread_loads.get(t, 0.0) / speed * _SCALE) * x[t, c]
                for t in range(num_threads)
            )
        )
        finish_vars.append(finish)
    makespan = model.NewIntVar(0, int(max_load / min(P_FREQ, E_FREQ) * _SCALE) + 1, "makespan")
    model.AddMaxEquality(makespan, finish_vars)

    # --- 通信局所性項 ---
    is_node0 = {}
    for t in range(num_threads):
        is_node0[t] = model.NewBoolVar(f"is_node0_{t}")
        model.Add(is_node0[t] == sum(x[t, c] for c in node_cores[0]))

    comm_scale = 1
    total_comm = sum(pairs.values()) or 1.0
    # 通信量もスケーリング（整数化のため正規化してから scale）
    comm_norm = {k: v / total_comm for k, v in pairs.items()}

    diff_vars = []
    diff_weights = []
    for (t1, t2), w in comm_norm.items():
        if w <= 0:
            continue
        diff = model.NewBoolVar(f"diff_{t1}_{t2}")
        model.Add(diff <= is_node0[t1] + is_node0[t2])
        model.Add(diff <= 2 - is_node0[t1] - is_node0[t2])
        model.Add(diff >= is_node0[t1] - is_node0[t2])
        model.Add(diff >= is_node0[t2] - is_node0[t1])
        diff_vars.append(diff)
        diff_weights.append(int(w * _SCALE))

    remote_penalty = model.NewIntVar(0, _SCALE, "remote_penalty")
    model.Add(remote_penalty == sum(w * d for w, d in zip(diff_weights, diff_vars)))

    # --- 目的関数: alpha*makespan(正規化) + (1-alpha)*remote_penalty(正規化) ---
    # makespanとremote_penaltyはスケールが違うので、本来は
    #   alpha*(makespan/makespan_max) + (1-alpha)*(remote_penalty/_SCALE)
    # を最小化したいが、CP-SATは変数式の除算(//)をサポートしないため、
    # 両辺に定数 makespan_max*_SCALE を掛けて除算を消した等価な式を最小化する
    # （定数倍・定数加算は最小化の解を変えない）。
    makespan_max = int(max_load / min(P_FREQ, E_FREQ) * _SCALE) + 1
    alpha_i = int(alpha * _SCALE)
    beta_i = _SCALE - alpha_i

    model.Minimize(alpha_i * _SCALE * makespan + beta_i * makespan_max * remote_penalty)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    cpu_map = [0] * num_threads
    for t in range(num_threads):
        for c in all_cores:
            if solver.Value(x[t, c]) == 1:
                cpu_map[t] = c
                break

    info = {
        "status": solver.StatusName(status),
        "makespan_scaled": solver.Value(makespan),
        "remote_penalty_scaled": solver.Value(remote_penalty),
        "wall_time_sec": solver.WallTime(),
    }
    return cpu_map, info


def compute_cpsat_map_from_csv(
    csv_path: str,
    num_threads: int,
    alpha: float = 0.5,
    use_loads: bool = True,
    time_limit_sec: float = 10.0,
) -> tuple[list[int], dict, dict | None]:
    mat = load_comm_matrix(csv_path)

    thread_loads = None
    imbalance = None
    mem_path = mem_access_path_for(csv_path)
    import os
    if use_loads and os.path.exists(mem_path):
        all_loads = load_mem_access(mem_path)
        thread_loads = {t: all_loads.get(t, 0.0) for t in range(num_threads)}
        imbalance = compute_load_imbalance(thread_loads)

    cpu_map, info = compute_cpsat_map(mat, num_threads, thread_loads=thread_loads, alpha=alpha, time_limit_sec=time_limit_sec)
    return cpu_map, info, imbalance


def _parse_args():
    p = argparse.ArgumentParser(description="CP-SATでcomm.csv/mem_access.csvからcpu_mapを同時最適化する")
    p.add_argument("csv_path", help="Data/comm_matrices/*.comm.csv")
    p.add_argument("--threads", type=int, required=True)
    p.add_argument("--alpha", type=float, default=0.5, help="0=局所性のみ, 1=ヘテロ性(makespan)のみ")
    p.add_argument("--no-loads", action="store_true", help="mem_access.csvを無視し全スレッド負荷=1で解く")
    p.add_argument("--time-limit", type=float, default=10.0)
    return p.parse_args()


def main():
    args = _parse_args()
    cpu_map, info, imbalance = compute_cpsat_map_from_csv(
        args.csv_path, args.threads,
        alpha=args.alpha, use_loads=not args.no_loads,
        time_limit_sec=args.time_limit,
    )
    print(f"cpu_map ({args.threads}TH, alpha={args.alpha}): {cpu_map}")
    for tid, cpu in enumerate(cpu_map):
        node = 0 if cpu < CORES_PER_NODE else 1
        ctype = "P" if cpu in NODE_P_CORES[node] else "E"
        print(f"  thread{tid:02d} -> CPU{cpu:02d} (Node{node} {ctype}-core)")
    print(f"\n[CP-SAT] status={info['status']}  makespan(scaled)={info['makespan_scaled']}  "
          f"remote_penalty(scaled)={info['remote_penalty_scaled']}  求解時間={info['wall_time_sec']:.3f}s")
    if imbalance:
        print(f"[負荷不均衡] imbalance_ratio={imbalance['imbalance_ratio']:.2%}  最重量=thread{imbalance['heaviest_tid']}")


if __name__ == "__main__":
    main()
