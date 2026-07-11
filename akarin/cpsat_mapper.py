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

定式化 (2026-07-11、roofline版に刷新。旧alpha数式は完全に置き換え):
  旧版は `alpha*makespan + (1-alpha)*remote_comm_penalty` という、秒(makespan)と
  無次元の通信量(remote_penalty)という単位の違う2つの量をalphaで無理やり線形
  混合していた。alphaの「正しい値」に理論的根拠がなく、7/6の実測検証でも
  remote_penalty項(別ノードだから悪いという前提)は実測と逆相関(-0.438)と判明。

  さらに7/10夜、First-Touch実装(v3)によりSniperのDRAM/バスモデルにノード単位の
  帯域競合という「本物の物理」が実装されたことを受け、alphaという恣意的な重みを
  廃し、3種のボトルネック候補のうち一番遅いものがそのまま完了時間になる、という
  ルーフラインモデルに全面置き換えた:

    node_finish[n]  = max(compute_bound[n], dram_bound[n])
    total_finish    = max(max_n(node_finish[n]), bus_bound)

  - compute_bound[n]: ノードn内で一番遅いスレッドの実行時間。
    mem_access.csv実測バイト数を「1バイト=1サイクル処理」という粗い前提で
    そのコアの周波数(Hz)で割り、秒に換算する(7/6にIPC実測ベースへの精緻化を
    試みたが逆に悪化(相関-0.308)したため、この粗い代理指標を維持)。
  - dram_bound[n]: ノードnに乗った全スレッドの合計メモリアクセス量
    (mem_access.csv実測バイト数の合計)÷ノードのDRAMコントローラ実帯域
    (PER_CONTROLLER_BANDWIDTH_GBPS、config/generate_config.py)。7/6に
    「別ノード通信への罰」より「同ノード帯域競合への罰」の方が実測と正しく
    相関する(+0.339)と検証済みの項をそのまま踏襲。
  - bus_bound: ノードをまたぐ通信量(comm_size.csv実測バイト数、システム全体で
    合算)÷ノード間バス実帯域(BUS_BANDWIDTH_GBPS)。バスはシステム全体で
    1本だけ共有(NetworkModelBusGlobal)なので、ノードペアごとではなく全体で
    1つのボトルネック候補として扱う(7/10夜、First-Touch実装で初めてノードを
    またぐアクセスに構造的コストが乗るようになったことを受けた新項)。

  全項を「秒」に統一しているため、alphaのような恣意的な重み付けが不要になった。

単体実行:
  python3 -m akarin.cpsat_mapper Data/tsuushin/sizeA/BT_A_12TH_bt.A.x.12.6.comm.csv --threads 12
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
    comm_size_path_for,
    compute_load_imbalance,
)
from config.generate_config import P_FREQ, E_FREQ, PER_CONTROLLER_BANDWIDTH_GBPS, BUS_BANDWIDTH_GBPS

# 2026-07-09: 以前はここに P_FREQ=4.0/E_FREQ=1.0 をハードコードし「generate_config.py
# と一致させる」とコメントするだけだった(手動同期に頼る設計)ため、generate_config.py
# 側が2026-07-06にi7-1195G7実測ベース(2.9/2.2GHz、P:E比1.318倍)へ変更された際に
# 追従できておらず、比4.0倍という全く違う前提でAKARINのCP-SAT最適化を解いていた
# ことが発覚(他セッションからの指摘で判明)。帯域定数も同じ理由で2026-07-11に
# generate_config.pyから直接importする形にした。単一の真実源から取ること。

# 秒をCP-SATの整数係数に変換するスケール。
# 2026-07-11: 当初10**9(ナノ秒相当)で実装したが、dram_coef = _SCALE/(51.2GB/s*1e9)
# ≈0.0195、bus_coef ≈0.00977 のようにどちらも1未満になり、int()切り捨てで
# 係数が0になってdram_bound/bus_boundが常に0という重大なバグを引き起こした
# (BT_S_8THで実際に発生、テスト実行で発覚)。帯域が「1秒あたり数十GB」という
# 大きい数のため、_SCALEをさらに大きくして係数が最低でも1以上の整数になるよう
# 桁を確保する必要がある。10**14で係数は十分な有効桁数(dram_coef≈1953)を持ち、
# かつcompute_upper等の上限もint64範囲(~9.2*10**18)に収まることを確認済み。
_SCALE = 10**14


def _core_speed(cpu_id: int) -> float:
    node = 0 if cpu_id < CORES_PER_NODE else 1
    return P_FREQ if cpu_id in NODE_P_CORES[node] else E_FREQ


def _node_of(cpu_id: int) -> int:
    return 0 if cpu_id < CORES_PER_NODE else 1


def compute_cpsat_map(
    comm_size_matrix: list[list[float]],
    num_threads: int,
    thread_loads: dict[int, float] | None = None,
    time_limit_sec: float = 10.0,
) -> tuple[list[int], dict]:
    """
    CP-SAT でルーフラインモデル(compute_bound/dram_bound/bus_bound の max())を
    最小化する cpu_map を返す。thread_loads が None の場合は全スレッド負荷=1。

    comm_size_matrix は comm.csv(メッセージ数)ではなく comm_size.csv(実バイト数)。
    dram_bound/bus_bound はどちらも「バイト数 ÷ 帯域(GB/s)」で秒を出す項なので、
    メッセージ数ではなくバイト数が必要。

    戻り値: (cpu_map, info) — info には目的関数値・compute_bound/dram_bound/
    bus_bound(いずれもスケール済み整数値、単位はナノ秒相当)を含む。
    """
    mat = comm_size_matrix[:num_threads]
    mat = [row[:num_threads] for row in mat]
    pairs = _pairs_from_matrix(mat)

    if thread_loads is None:
        thread_loads = {t: 1.0 for t in range(num_threads)}

    node_cores = {0: NODE_P_CORES[0] + NODE_E_CORES[0], 1: NODE_P_CORES[1] + NODE_E_CORES[1]}
    all_cores = node_cores[0] + node_cores[1]
    total_load = sum(thread_loads.get(t, 0.0) for t in range(num_threads))

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

    # ノード所属(is_node[n][t] = 1 ならスレッドtはノードnに乗っている)
    is_node = {0: {}, 1: {}}
    for t in range(num_threads):
        is_node[0][t] = model.NewBoolVar(f"is_node0_{t}")
        model.Add(is_node[0][t] == sum(x[t, c] for c in node_cores[0]))
        is_node[1][t] = model.NewBoolVar(f"is_node1_{t}")
        model.Add(is_node[1][t] == sum(x[t, c] for c in node_cores[1]))

    # コンパイル時定数の帯域係数(秒 * _SCALE を1バイトあたりに換算)
    dram_coef = int(_SCALE / (PER_CONTROLLER_BANDWIDTH_GBPS * 1e9))
    bus_coef  = int(_SCALE / (BUS_BANDWIDTH_GBPS * 1e9))

    compute_upper = int(total_load / (min(P_FREQ, E_FREQ) * 1e9) * _SCALE) + 1
    dram_upper = int(total_load * dram_coef) + 1

    node_finish_vars = []
    compute_bound_vars = []
    dram_bound_vars = []
    for n in (0, 1):
        # --- compute_bound[n]: ノード内で一番遅いスレッドの実行時間 ---
        finish_vars = []
        for c in node_cores[n]:
            speed_hz = _core_speed(c) * 1e9
            coef = {t: int(thread_loads.get(t, 0.0) / speed_hz * _SCALE) for t in range(num_threads)}
            finish = model.NewIntVar(0, compute_upper, f"finish_{c}")
            model.Add(finish == sum(coef[t] * x[t, c] for t in range(num_threads)))
            finish_vars.append(finish)
        compute_bound_n = model.NewIntVar(0, compute_upper, f"compute_bound_{n}")
        model.AddMaxEquality(compute_bound_n, finish_vars)
        compute_bound_vars.append(compute_bound_n)

        # --- dram_bound[n]: ノードn全スレッドの合計メモリアクセス量 ÷ DRAM帯域 ---
        dram_bound_n = model.NewIntVar(0, dram_upper, f"dram_bound_{n}")
        model.Add(
            dram_bound_n
            == sum(int(thread_loads.get(t, 0.0) * dram_coef) * is_node[n][t] for t in range(num_threads))
        )
        dram_bound_vars.append(dram_bound_n)

        node_finish_n = model.NewIntVar(0, max(compute_upper, dram_upper), f"node_finish_{n}")
        model.AddMaxEquality(node_finish_n, [compute_bound_n, dram_bound_n])
        node_finish_vars.append(node_finish_n)

    # --- bus_bound: ノードをまたぐ通信量(システム全体で合算) ÷ バス帯域 ---
    # バスはNetworkModelBusGlobalとしてシステム全体で1本のみ共有されるため、
    # ノードペアごとではなく全体の合算値を1つのボトルネック候補として扱う。
    total_bytes = sum(pairs.values()) or 0.0
    bus_upper = int(total_bytes * bus_coef) + 1

    diff_terms = []
    for (t1, t2), bytes_w in pairs.items():
        if bytes_w <= 0:
            continue
        diff = model.NewBoolVar(f"diff_{t1}_{t2}")
        model.Add(diff <= is_node[0][t1] + is_node[0][t2])
        model.Add(diff <= 2 - is_node[0][t1] - is_node[0][t2])
        model.Add(diff >= is_node[0][t1] - is_node[0][t2])
        model.Add(diff >= is_node[0][t2] - is_node[0][t1])
        diff_terms.append(int(bytes_w * bus_coef) * diff)

    bus_bound = model.NewIntVar(0, bus_upper, "bus_bound")
    model.Add(bus_bound == sum(diff_terms))

    # --- 目的関数: 3種のボトルネック候補のうち最も遅いものを最小化 ---
    # alphaのような恣意的な重み付けは不要(ルーフラインモデル、7/6+7/10設計)。
    total_upper = max(compute_upper, dram_upper, bus_upper)
    total_finish = model.NewIntVar(0, total_upper, "total_finish")
    model.AddMaxEquality(total_finish, node_finish_vars + [bus_bound])
    model.Minimize(total_finish)

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
        "total_finish_scaled": solver.Value(total_finish),
        "compute_bound_scaled": [solver.Value(v) for v in compute_bound_vars],
        "dram_bound_scaled": [solver.Value(v) for v in dram_bound_vars],
        "bus_bound_scaled": solver.Value(bus_bound),
        "wall_time_sec": solver.WallTime(),
    }
    return cpu_map, info


def compute_cpsat_map_from_csv(
    csv_path: str,
    num_threads: int,
    use_loads: bool = True,
    time_limit_sec: float = 10.0,
) -> tuple[list[int], dict, dict | None]:
    """
    csv_path は comm.csv(メッセージ数)のパスを受け取るが、実際の最適化には
    同じ実行が出力した comm_size.csv(実バイト数)を使う(bus_boundがバイト数
    ÷帯域で秒を出すため)。comm_size.csvが存在しない場合はcomm.csvのメッセージ数を
    代用する(単位は不正確になるが、少なくとも「多い/少ない」の相対順序は保たれる)。
    """
    import os

    size_path = comm_size_path_for(csv_path)
    mat = load_comm_matrix(size_path) if os.path.exists(size_path) else load_comm_matrix(csv_path)

    thread_loads = None
    imbalance = None
    mem_path = mem_access_path_for(csv_path)
    if use_loads and os.path.exists(mem_path):
        all_loads = load_mem_access(mem_path)
        thread_loads = {t: all_loads.get(t, 0.0) for t in range(num_threads)}
        imbalance = compute_load_imbalance(thread_loads)

    cpu_map, info = compute_cpsat_map(mat, num_threads, thread_loads=thread_loads, time_limit_sec=time_limit_sec)
    return cpu_map, info, imbalance


def _parse_args():
    p = argparse.ArgumentParser(description="CP-SAT(ルーフラインモデル)でcomm_size.csv/mem_access.csvからcpu_mapを最適化する")
    p.add_argument("csv_path", help="Data/tsuushin/size{class}/*.comm.csv")
    p.add_argument("--threads", type=int, required=True)
    p.add_argument("--no-loads", action="store_true", help="mem_access.csvを無視し全スレッド負荷=1で解く")
    p.add_argument("--time-limit", type=float, default=10.0)
    return p.parse_args()


def main():
    args = _parse_args()
    cpu_map, info, imbalance = compute_cpsat_map_from_csv(
        args.csv_path, args.threads,
        use_loads=not args.no_loads,
        time_limit_sec=args.time_limit,
    )
    print(f"cpu_map ({args.threads}TH): {cpu_map}")
    for tid, cpu in enumerate(cpu_map):
        node = 0 if cpu < CORES_PER_NODE else 1
        ctype = "P" if cpu in NODE_P_CORES[node] else "E"
        print(f"  thread{tid:02d} -> CPU{cpu:02d} (Node{node} {ctype}-core)")
    print(f"\n[CP-SAT] status={info['status']}  total_finish(scaled)={info['total_finish_scaled']}  "
          f"compute_bound(scaled)={info['compute_bound_scaled']}  dram_bound(scaled)={info['dram_bound_scaled']}  "
          f"bus_bound(scaled)={info['bus_bound_scaled']}  求解時間={info['wall_time_sec']:.3f}s")
    if imbalance:
        print(f"[負荷不均衡] imbalance_ratio={imbalance['imbalance_ratio']:.2%}  最重量=thread{imbalance['heaviest_tid']}")


if __name__ == "__main__":
    main()
