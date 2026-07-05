"""
deloc_mapper.py
Agung et al. (IEEE Access 2020) の DeLoc アルゴリズムを、Purple サーバ上の
detloc-tracer で取得した実通信行列 (Data/comm_matrices/*.comm.csv) から
本プロジェクトの NUMA トポロジ (2 node x (4 P-core + 4 E-core)) 向けの
cpu_map へ変換する。

アルゴリズムの出典:
  - Agung の DeLoc 本体 (ノード決定のみ):
    /home/agungm/github/ProcessMapping/spatial_deloc.py の
    SpatialDeloc.calc_map_pair(weight_type=0)  (「DeLoc: balanced and locality by pairs」)
    openmpi-3.1.4-deloc の deloc_map.c map_deloc() と設計が一致（独立2実装で確認済み）
  - Jin の修士論文 3.3.1 節「Memory-aware Priority Option (MPO)」:
    Agung の DeLoc（ノード決定）に、ノード内でのヘテロ性考慮ステップを追加した拡張。
    論文原文:
      Step1: "the mapping strategy will pair individual tasks according to the
              communication behavior... task pairs... will be mapped to the same
              NUMA node."  (= Agung の DeLoc そのもの、ノード決定のみ)
      Step2: "the mapping strategy will take into account the core heterogeneity
              factor by mapping high-load tasks to Big cores and low-load tasks
              to Small cores within a NUMA node."
      論文の例で明記される制約: "since there are only two Big cores on each node,
      MPO has to assign one of the high-load tasks to a Small core." つまり
      ノード所属は Step1 で確定済みのまま動かさず、ノード内の Big core 数を
      超えた分の重いタスクは Small core に落ちる（これが MPO が HPO に劣る
      構造的要因）。本実装はこの 2 ステップをそのまま再現する。

MPO (2ステップ) のロジック:
  Step1 (ノード決定, weight_type=0):
    1. スレッドペアの通信量を降順にソート
    2. ペアを順に処理し、両ノードへ交互 (round-robin) に貪欲配置
    3. 各ノードは ceil(num_threads / num_nodes) スレッドで「ソフト満杯」とみなし
       以降のペアは次ノードへ回す（is_avail 相当）
  Step2 (ノード内 Big/Small 割当。thread_loads 指定時のみ):
    4. Step1 で確定した各ノードの所属スレッドを、実測負荷 (mem_access.csv) の
       降順にソートし、そのノードの Big(P) core 数までを Big core へ、
       残りを Small(E) core へ割り当てる。ノード所属は変更しない。

detloc-tracer の CSV は行 0 が最大スレッド ID に対応する逆順インデックスなので
(mat_loader.py の is_deloc_form=True 相当)、読み込み時に real_ti = n-1-i で補正する。

スレッド負荷不均衡（Jin の修士論文 Table 4.3「Task Load Difference」相当）:
  detloc-tracer は comm.csv と同じ実行から mem_access.csv
  (tid, n_reads, n_writes, sz_reads, sz_writes) も出力する。これは DeLoc の
  通信行列には現れない、各スレッド自身の実測メモリアクセス量（＝計算負荷の代理指標）。
  compute_deloc_map_from_csv() は comm.csv と同名の mem_access.csv が
  存在すれば自動で読み込み、不均衡度 (imbalance_ratio = max/mean - 1) を常に記録する。
  use_load_weighting=True のときのみ Step2 (Big/Small 再配置) を実際の配置に反映する。

  未実装の検討事項（ノード制約の緩和）:
  Jin の MPO はノード所属を Step1 で固定するため、重いタスクが同じノードに
  偏ると Big core 不足で Small core に落ちる（論文が明記する構造的欠陥）。
  この制約を緩和する（他ノードに空き Big core があれば越境させる等）改良は
  Jin の原著には存在せず、意図的に未実装のままにしている。

単体実行:
  python3 -m utility.deloc_mapper Data/comm_matrices/BT_2TH_bt.S.x.2.6.comm.csv --threads 2
"""

import argparse
import csv
import glob
import math
import os
from typing import Optional

COMM_MATRICES_DIR = "/home/hiragahama/ClaudeXSniper/Data/comm_matrices"


def find_comm_csv(workload: str, bench_class: str, num_threads: int) -> str:
    """Data/comm_matrices/ から該当する comm.csv のパスを探す。"""
    if bench_class == "S":
        pattern = f"{COMM_MATRICES_DIR}/{workload}_{num_threads}TH_*.comm.csv"
    else:
        pattern = f"{COMM_MATRICES_DIR}/{workload}_{bench_class}_{num_threads}TH_*.comm.csv"
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(f"comm.csv が見つかりません: {pattern}")
    return matches[0]

NUM_NODES = 2
CORES_PER_NODE = 8
# ノード内の埋める順序: P-core (4本) を先に、E-core (4本) を後に
NODE_CORE_ORDER = {
    0: [0, 1, 2, 3, 4, 5, 6, 7],
    1: [8, 9, 10, 11, 12, 13, 14, 15],
}
# ノードごとの Big(P)/Small(E) core 一覧（Jin 3.3.1 Step2 の再現に使用）
NODE_P_CORES = {0: [0, 1, 2, 3], 1: [8, 9, 10, 11]}
NODE_E_CORES = {0: [4, 5, 6, 7], 1: [12, 13, 14, 15]}


def load_comm_matrix(csv_path: str) -> list[list[float]]:
    """detloc-tracer 出力の comm.csv を読み込む（is_deloc_form、行反転なし）。"""
    with open(csv_path, newline="") as f:
        rows = [[float(x) for x in row] for row in csv.reader(f) if row]
    return rows


def _pairs_from_matrix(mat: list[list[float]]) -> dict[tuple[int, int], float]:
    """
    行列から (real_t1, t2) -> comm量 のペア辞書を作る。
    detloc-tracer の行 i は実スレッド (n-1-i) に対応する（逆順）。
    """
    n = len(mat)
    pairs: dict[tuple[int, int], float] = {}
    for i in range(n):
        real_ti = n - 1 - i
        for j in range(real_ti):
            pairs[(real_ti, j)] = mat[i][j]
    return pairs


def mem_access_path_for(comm_csv_path: str) -> str:
    """comm.csv と同じ detloc-tracer 実行が出力した mem_access.csv のパスを推定する。"""
    return comm_csv_path.replace(".comm.csv", ".mem_access.csv")


def load_mem_access(csv_path: str) -> dict[int, float]:
    """
    detloc-tracer 出力の mem_access.csv (tid,n_reads,n_writes,sz_reads,sz_writes) を
    読み込み、各スレッドの実測メモリアクセス量（バイト = sz_reads+sz_writes）を返す。
    DeLoc の通信行列には現れない、スレッド自身の計算負荷の代理指標。
    """
    loads: dict[int, float] = {}
    with open(csv_path, newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            tid = int(row[0])
            sz_reads, sz_writes = float(row[3]), float(row[4])
            loads[tid] = sz_reads + sz_writes
    return loads


def compute_load_imbalance(thread_loads: dict[int, float]) -> dict:
    """スレッド間の負荷不均衡を表す指標を計算する（Jin 修士論文 Table 4.3 相当）。"""
    values = list(thread_loads.values())
    mean = sum(values) / len(values)
    max_load = max(values)
    heaviest_tid = max(thread_loads, key=thread_loads.get)
    lightest_tid = min(thread_loads, key=thread_loads.get)
    return {
        "mean": mean,
        "max": max_load,
        "min": min(values),
        "heaviest_tid": heaviest_tid,
        "lightest_tid": lightest_tid,
        "imbalance_ratio": (max_load / mean - 1) if mean > 0 else 0.0,
    }


def _apply_heterogeneity_within_node(
    cpu_map: list[int],
    thread_loads: dict[int, float],
) -> list[int]:
    """
    Jin 修士論文 3.3.1 節 Step2 の再現。
    Step1 (DeLoc) で確定済みのノード所属は変えず、各ノード内の所属スレッドを
    実測負荷の降順に並べ、そのノードの Big(P) core 数までを Big core へ、
    残りを Small(E) core へ割り当てる。
    ノード内に Big core の空きより多い高負荷スレッドが集まっていれば、
    論文が明記する通り、あふれた分は Small core に落ちる（意図的な再現）。
    """
    num_threads = len(cpu_map)
    node_members: dict[int, list[int]] = {0: [], 1: []}
    for tid, cpu in enumerate(cpu_map):
        node = 0 if cpu < CORES_PER_NODE else 1
        node_members[node].append(tid)

    new_cpu_map = [0] * num_threads
    for node_id, members in node_members.items():
        members_sorted = sorted(members, key=lambda t: thread_loads.get(t, 0.0), reverse=True)
        p_slots = list(NODE_P_CORES[node_id])
        e_slots = list(NODE_E_CORES[node_id])
        for tid in members_sorted:
            if p_slots:
                new_cpu_map[tid] = p_slots.pop(0)
            else:
                new_cpu_map[tid] = e_slots.pop(0)

    return new_cpu_map


class _NumaMach:
    """2 node x (4P+4E) トポロジ上での貪欲配置状態を保持する。"""

    def __init__(self, num_threads: int):
        self.num_threads = num_threads
        self.bal_per_node = math.ceil(num_threads / NUM_NODES)
        self.used_in_node = [0, 0]
        self.task_cpu: dict[int, int] = {}

    def is_avail(self, node_id: int) -> bool:
        return self.used_in_node[node_id] < self.bal_per_node

    def next_node(self, node_id: int) -> int:
        return (node_id + 1) % NUM_NODES

    def map_next(self, tid: int, node_id: int) -> bool:
        """node_id 内の次の空きコア（P 優先）に tid を配置する。"""
        used = self.used_in_node[node_id]
        if used >= CORES_PER_NODE:
            return False
        cpu = NODE_CORE_ORDER[node_id][used]
        self.task_cpu[tid] = cpu
        self.used_in_node[node_id] += 1
        return True

    def map_pair_task(self, tid: int, start_node: int, node_loads: list[float], pernode_quo: float) -> int:
        """DeLoc の _map_pair 相当。既配置ならそのノードを返す。"""
        if tid in self.task_cpu:
            return 0 if self.task_cpu[tid] in NODE_CORE_ORDER[0] else 1

        target = start_node
        if not self.is_avail(target) or node_loads[target] >= pernode_quo:
            target = self.next_node(target)

        tries = 0
        while not self.map_next(tid, target) and tries < NUM_NODES:
            target = self.next_node(target)
            tries += 1
        return target


def compute_deloc_map(
    comm_matrix: list[list[float]],
    num_threads: int,
    thread_loads: Optional[dict[int, float]] = None,
) -> list[int]:
    """
    MPO (Jin 3.3.1節) を適用し、thread_id -> cpu_id の対応リスト (index=thread_id) を返す。

    Step1: 通信量のみでノード決定 (Agung の DeLoc, weight_type=0)。
    Step2: thread_loads が与えられた場合のみ、Step1 のノード所属を変えずに
           ノード内で負荷降順に Big(P)→Small(E) へ再配置する。
           thread_loads が None なら Step1 の結果（P-core 優先の詰め込み）をそのまま返す。
    """
    mat = comm_matrix[:num_threads]
    mat = [row[:num_threads] for row in mat]
    pairs = _pairs_from_matrix(mat)

    # Step1: ノード決定（通信量降順、負荷は一切見ない）
    sorted_pairs = sorted(pairs.items(), key=lambda kv: kv[1], reverse=True)

    mach = _NumaMach(num_threads)
    node_loads = [0.0, 0.0]
    load_total = sum(pairs.values())
    pernode_quo = load_total / NUM_NODES if load_total > 0 else 0.0

    for inc, ((t1, t2), weight) in enumerate(sorted_pairs):
        half_load = weight / 2
        curr_node = inc % NUM_NODES
        t1_node = mach.map_pair_task(t1, curr_node, node_loads, pernode_quo)
        t2_node = mach.map_pair_task(t2, t1_node, node_loads, pernode_quo)
        node_loads[t1_node] += half_load
        node_loads[t2_node] += half_load

    # 通信を持たない（孤立した）スレッドが残っていれば負荷最小ノードに詰める
    for tid in range(num_threads):
        if tid not in mach.task_cpu:
            target = node_loads.index(min(node_loads))
            while not mach.map_next(tid, target):
                target = mach.next_node(target)

    cpu_map = [mach.task_cpu[tid] for tid in range(num_threads)]

    # Step2: ノード内 Big/Small 再配置（ノード所属自体は変更しない）
    if thread_loads:
        cpu_map = _apply_heterogeneity_within_node(cpu_map, thread_loads)

    return cpu_map


def compute_deloc_map_from_csv(
    csv_path: str,
    num_threads: int,
    use_load_weighting: bool = True,
) -> tuple[list[int], Optional[dict]]:
    """
    cpu_map を計算する。

    既定 (use_load_weighting=True) は Jin 修士論文 3.3.1節の MPO をそのまま再現する:
    Step1 (Agung の DeLoc、通信量のみでノード決定) + Step2 (ノード内で負荷降順に
    Big→Small へ再配置)。mem_access.csv が同じ場所に無ければ Step2 は自動的に
    スキップされ、Step1 のみ（Agung の元の DeLoc、weight_type=0）になる。

    use_load_weighting=False にすると、mem_access.csv があっても Step2 を
    強制的にスキップする（Agung の元の DeLoc 単体、あるいは HPO 等ナイーブな
    戦略と比較する場合など、意図的に Step1 のみを見たいときに使う）。

    mem_access.csv が見つかれば、Step2 を使うか否かに関わらず
    不均衡情報 (compute_load_imbalance) は常に計算して返す（診断用）。
    """
    mat = load_comm_matrix(csv_path)

    thread_loads = None
    imbalance = None
    mem_path = mem_access_path_for(csv_path)
    if os.path.exists(mem_path):
        all_loads = load_mem_access(mem_path)
        loads_for_range = {t: all_loads.get(t, 0.0) for t in range(num_threads)}
        imbalance = compute_load_imbalance(loads_for_range)
        if use_load_weighting:
            thread_loads = loads_for_range

    cpu_map = compute_deloc_map(mat, num_threads, thread_loads=thread_loads)
    return cpu_map, imbalance


def _parse_args():
    p = argparse.ArgumentParser(description="DeLoc/MPO アルゴリズムで comm.csv から cpu_map を計算する")
    p.add_argument("csv_path", help="Data/comm_matrices/*.comm.csv")
    p.add_argument("--threads", type=int, required=True, help="スレッド数")
    p.add_argument("--node-only", action="store_true",
                   help="Step1 (Agung の DeLoc, ノード決定) のみ。Step2 (ノード内 Big/Small 再配置) をスキップする")
    return p.parse_args()


def main():
    args = _parse_args()
    cpu_map, imbalance = compute_deloc_map_from_csv(
        args.csv_path, args.threads,
        use_load_weighting=not args.node_only,
    )
    print(f"cpu_map ({args.threads}TH): {cpu_map}")
    for tid, cpu in enumerate(cpu_map):
        node = 0 if cpu < 8 else 1
        ctype = "P" if (cpu % 8) < 4 else "E"
        heavy = ""
        if imbalance and tid == imbalance["heaviest_tid"]:
            heavy = "  <- 最重量スレッド"
        elif imbalance and tid == imbalance["lightest_tid"]:
            heavy = "  <- 最軽量スレッド"
        print(f"  thread{tid:02d} -> CPU{cpu:02d} (Node{node} {ctype}-core){heavy}")

    mode = "Step1のみ (Agung DeLoc, ノード決定だけ)" if args.node_only else "Step1+Step2 (Jin MPO 完全版)"
    if imbalance:
        print(f"\n[負荷不均衡] mean={imbalance['mean']:.0f}  max={imbalance['max']:.0f}  "
              f"imbalance_ratio={imbalance['imbalance_ratio']:.2%}  "
              f"(最重量=thread{imbalance['heaviest_tid']})  [計算モード: {mode}]")
    else:
        print(f"\n[負荷不均衡] mem_access.csv が見つかりません  [計算モード: {mode}]")


if __name__ == "__main__":
    main()
