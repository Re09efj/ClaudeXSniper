"""
stats_reader.py
Sniper の sim.stats.sqlite3 から統計を読み取る。

単位メモ:
  barrier/global_time, performance_model/elapsed_time → フェムト秒 (fs = 1e-15 s)
  dram/total-access-latency → fs
  L1-D/loads-where-* → ロード回数 (キャッシュ階層別ヒット数)
"""

import os
import sqlite3


P_CORES    = set(range(0, 4)) | set(range(8, 12))   # CPU 0-3, 8-11 (P-core 4GHz)
E_CORES    = set(range(4, 8)) | set(range(12, 16))  # CPU 4-7, 12-15 (E-core 1GHz)
NODE0_CPUS = set(range(0, 8))
NODE1_CPUS = set(range(8, 16))

P_FREQ_HZ = 4.0e9
E_FREQ_HZ = 1.0e9


def _db(output_dir: str):
    path = os.path.join(output_dir, "sim.stats.sqlite3")
    if not os.path.exists(path):
        return None
    return sqlite3.connect(path)


def _query(conn, prefix: str, obj: str, metric: str) -> list:
    """指定の prefix/objectname/metricname の (core, value) リストを返す。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.core, v.value
        FROM "values" v
        JOIN prefixes p ON v.prefixid = p.prefixid
        JOIN names n    ON v.nameid   = n.nameid
        WHERE p.prefixname = ?
          AND n.objectname = ?
          AND n.metricname = ?
        ORDER BY v.core
        """,
        (prefix, obj, metric),
    )
    return cur.fetchall()


# ─── シミュレーション時間 ────────────────────────────────────────

def parse_sim_time(output_dir: str) -> float | None:
    """
    シミュレーション時間(秒)を返す。barrier/global_time から算出。
    global_time の単位はフェムト秒 (fs = 1e-15 s)。
    """
    conn = _db(output_dir)
    if conn is None:
        return None
    rows = _query(conn, "stop", "barrier", "global_time")
    conn.close()
    if not rows:
        return None
    fs = rows[0][1]
    return fs / 1e15   # fs → seconds


# ─── DRAM アクセス (NUMA ノード別) ───────────────────────────────

def parse_node_stats(output_dir: str, num_nodes: int = 2, cpu_map: list | None = None) -> dict:
    """
    NUMA ノード別 DRAM アクセス数を返す。
    dram/reads, dram/writes の core フィールドは DRAM controller (= ノード) ID。
    dram/writes は Sniper がライトバックをカウントしないため 0 が返る場合がある。

    その場合、L1-D/stores-where-dram-local/remote (シミュレート対象コア単位) から
    ノード別に再構成する。あるコアの "local" store はそのコアの所属ノードの
    コントローラへ、"remote" store はもう一方のノードのコントローラへの書き込みなので、
    ノード N の書き込み数 = (ノード N 所属コアの local 合計) + (それ以外のコアの remote 合計)。
    cpu_map が無い場合はコア→ノード対応が取れないため、この再構成は行わない。

    Returns: {node_id: {"reads": int, "writes": int}}
    """
    conn = _db(output_dir)
    if conn is None:
        print(f"[WARN] sim.stats.sqlite3 が見つかりません: {output_dir}")
        return {}

    reads_map  = {r[0]: r[1] for r in _query(conn, "stop", "dram", "reads")}
    writes_map = {r[0]: r[1] for r in _query(conn, "stop", "dram", "writes")}

    # L1-D stores-where-dram-local/remote (シミュレート対象コア単位、フォールバック用)
    store_local_per_core  = dict(_query(conn, "stop", "L1-D", "stores-where-dram-local"))
    store_remote_per_core = dict(_query(conn, "stop", "L1-D", "stores-where-dram-remote"))

    conn.close()

    # Sniper のコントローラ ID はマスターコアのインデックス (0, N, 2N, ...) のため、
    # reads_map のキーを昇順ソートして NUMA ノード 0, 1, ... に対応付ける。
    ctrl_ids = sorted(reads_map.keys())

    result = {}
    for node in range(num_nodes):
        ctrl_id     = ctrl_ids[node] if node < len(ctrl_ids) else None
        dram_reads  = reads_map.get(ctrl_id, 0) if ctrl_id is not None else 0
        dram_writes = writes_map.get(ctrl_id, 0) if ctrl_id is not None else 0
        result[node] = {"reads": dram_reads, "writes": dram_writes}

    if cpu_map:
        sim_cores = set(store_local_per_core) | set(store_remote_per_core)
        node_of = {
            sim_core: (0 if cpu_map[sim_core] in NODE0_CPUS else 1)
            for sim_core in sim_cores
            if sim_core < len(cpu_map)
        }
        for node in range(num_nodes):
            if result[node]["writes"] != 0:
                continue
            total = 0
            for sim_core, core_node in node_of.items():
                if core_node == node:
                    total += store_local_per_core.get(sim_core, 0)
                else:
                    total += store_remote_per_core.get(sim_core, 0)
            result[node]["writes"] = total

    return result


# ─── NUMA アクセス詳細 ──────────────────────────────────────────

def parse_numa_access(output_dir: str) -> dict:
    """
    L1-D loads-where-dram-local / remote からローカル・リモートアクセス数を返す。

    Returns:
      {"local": int, "remote": int, "per_core": {core_id: {"local":, "remote":}}}
    """
    conn = _db(output_dir)
    if conn is None:
        return {"local": 0, "remote": 0, "per_core": {}}

    local_rows  = _query(conn, "stop", "L1-D", "loads-where-dram-local")
    remote_rows = _query(conn, "stop", "L1-D", "loads-where-dram-remote")
    conn.close()

    per_core: dict = {}
    for core, val in local_rows:
        per_core.setdefault(core, {"local": 0, "remote": 0})["local"] = val
    for core, val in remote_rows:
        per_core.setdefault(core, {"local": 0, "remote": 0})["remote"] = val

    total_local  = sum(v["local"]  for v in per_core.values())
    total_remote = sum(v["remote"] for v in per_core.values())
    return {"local": total_local, "remote": total_remote, "per_core": per_core}


# ─── 命令数・IPC・サイクル ──────────────────────────────────────

def parse_instructions(output_dir: str) -> dict:
    """コアごとの命令数を返す。{core_id: int}"""
    conn = _db(output_dir)
    if conn is None:
        return {}
    rows = _query(conn, "stop", "core", "instructions")
    conn.close()
    return {r[0]: r[1] for r in rows}


def parse_elapsed_time_fs(output_dir: str) -> dict:
    """
    コアごとの経過シミュレーション時間 (フェムト秒) を返す。
    {core_id: fs}
    performance_model/elapsed_time から取得。
    """
    conn = _db(output_dir)
    if conn is None:
        return {}
    rows = _query(conn, "stop", "performance_model", "elapsed_time")
    conn.close()
    return {r[0]: r[1] for r in rows}


def parse_cycles(output_dir: str, cpu_map: list | None = None) -> dict:
    """
    コアごとのサイクル数を返す。{core_id: int}
    elapsed_time (fs) × 周波数 (Hz) で計算。
    cpu_map が与えられれば P/E コア周波数を使い分ける。
    """
    elapsed = parse_elapsed_time_fs(output_dir)
    if not elapsed:
        return {}

    result = {}
    for sim_core, fs in elapsed.items():
        if cpu_map and sim_core < len(cpu_map):
            cpu_id = cpu_map[sim_core]
            freq = P_FREQ_HZ if cpu_id in P_CORES else E_FREQ_HZ
        else:
            freq = P_FREQ_HZ  # デフォルト: P-core
        result[sim_core] = int(fs * 1e-15 * freq)

    return result


def parse_ipc(output_dir: str, cpu_map: list | None = None) -> dict:
    """
    コアごとの IPC を返す。{core_id: float}
    IPC = instructions / cycles
    """
    inst_map  = parse_instructions(output_dir)
    cycle_map = parse_cycles(output_dir, cpu_map)

    result = {}
    for core, insts in inst_map.items():
        cycles = cycle_map.get(core, 0)
        if cycles > 0 and insts > 0:
            result[core] = insts / cycles
    return result


# ─── L1-D キャッシュ階層内訳 ────────────────────────────────────

def parse_l1d_where(output_dir: str) -> dict:
    """
    L1-D loads-where-* でキャッシュ階層別ヒット数を返す。

    Returns: {core_id: {"l1": int, "l1s": int, "l2": int,
                         "l3": int, "l3s": int,
                         "dram_local": int, "dram_remote": int}}

    metric name は大文字 (loads-where-L1, loads-where-L2, loads-where-L3_S 等)。
    """
    conn = _db(output_dir)
    if conn is None:
        return {}

    metrics = {
        "l1":          "loads-where-L1",
        "l1s":         "loads-where-L1_S",
        "l2":          "loads-where-L2",
        "l3":          "loads-where-L3",
        "l3s":         "loads-where-L3_S",
        "dram_local":  "loads-where-dram-local",
        "dram_remote": "loads-where-dram-remote",
    }

    per_core: dict[int, dict] = {}
    for key, metric in metrics.items():
        for core, val in _query(conn, "stop", "L1-D", metric):
            per_core.setdefault(core, {k: 0 for k in metrics})[key] = val

    conn.close()
    return per_core


# ─── interval_timer CPI 内訳 ─────────────────────────────────────

def parse_cpi_breakdown(output_dir: str) -> dict:
    """
    interval_timer の CPI 内訳を返す。
    値は「サイクル × 1000000000000」の固定小数点 (fs単位累積)。

    Returns: {core_id: {"base": int, "branch": int, "l2": int,
                         "l3": int, "dram_local": int, "dram_remote": int,
                         "l3s": int}}
    """
    conn = _db(output_dir)
    if conn is None:
        return {}

    metrics = {
        "base":        "cpiBase",
        "branch":      "cpiBranchPredictor",
        "l2":          "cpiDataCacheL2",
        "l3":          "cpiDataCacheL3",
        "l3s":         "cpiDataCacheL3_S",
        "dram_local":  "cpiDataCachedram-local",
        "dram_remote": "cpiDataCachedram-remote",
    }

    per_core: dict[int, dict] = {}
    for key, metric in metrics.items():
        for core, val in _query(conn, "stop", "interval_timer", metric):
            per_core.setdefault(core, {k: 0 for k in metrics})[key] = val

    conn.close()
    return per_core


# ─── 同期統計 ───────────────────────────────────────────────────

def parse_sync_stats(output_dir: str) -> dict:
    """
    futex / pthread 同期カウンタを返す。

    Returns: {
        "futex_wake_count":      int,   # futex_wake 呼び出し回数 (全コア合計)
        "futex_wait_count":      int,
        "mutex_lock_count":      int,   # pthread_mutex_lock 回数
        "barrier_wait_count":    int,   # pthread_barrier_wait 回数
        "futex_wake_per_minst":  float, # wake 回数 / 100万命令
        "mutex_lock_per_minst":  float,
        "barrier_wait_per_minst": float,
    }
    """
    conn = _db(output_dir)
    if conn is None:
        return {}

    def _sum(obj: str, metric: str) -> int:
        rows = _query(conn, "stop", obj, metric)
        return sum(v for _, v in rows) if rows else 0

    futex_wake   = _sum("futex",  "futex_wake_count")
    futex_wait   = _sum("futex",  "futex_wait_count")
    mutex_lock   = _sum("pthread","pthread_mutex_lock_count")
    barrier_wait = _sum("pthread","pthread_barrier_wait_count")

    # 命令数 (performance_model 単一値)
    insts_rows = _query(conn, "stop", "performance_model", "instruction_count")
    total_insts = insts_rows[0][1] if insts_rows else 0
    conn.close()

    per_m = lambda n: n / (total_insts / 1e6) if total_insts > 0 else 0.0

    return {
        "futex_wake_count":       futex_wake,
        "futex_wait_count":       futex_wait,
        "mutex_lock_count":       mutex_lock,
        "barrier_wait_count":     barrier_wait,
        "futex_wake_per_minst":   round(per_m(futex_wake),   4),
        "mutex_lock_per_minst":   round(per_m(mutex_lock),   4),
        "barrier_wait_per_minst": round(per_m(barrier_wait), 4),
    }


# ─── 全生指標ダンプ ─────────────────────────────────────────────

def dump_all_stats(output_dir: str, cpu_map: list | None = None) -> dict:
    """
    sim.stats.sqlite3 の prefix='stop' 全指標をフラット dict で返す。

    命名規則:
      単一値   → "{obj}.{metric}"
      複数コア → "{obj}.{metric}.c{N}"  (N = Sniper コア/コントローラ index)
               + "{obj}.{metric}.total"
               + "{obj}.{metric}.node0" / ".node1"  (cpu_map があれば正確、なければ
                 ソート順で最初のコア群=node0)

    Returns: dict[str, int|float]  ※ キーはすべて str
    """
    conn = _db(output_dir)
    if conn is None:
        return {}

    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT n.objectname, n.metricname
        FROM   "values" v
        JOIN   prefixes p ON v.prefixid = p.prefixid
        JOIN   names    n ON v.nameid   = n.nameid
        WHERE  p.prefixname = 'stop'
        ORDER  BY n.objectname, n.metricname
        """
    )
    pairs = cur.fetchall()

    # sim_core → node id (cpu_map ベース)
    core_to_node: dict[int, int] = {}
    if cpu_map:
        for sim_core, cpu_id in enumerate(cpu_map):
            core_to_node[sim_core] = 0 if cpu_id in NODE0_CPUS else 1
    max_sim_core = (len(cpu_map) - 1) if cpu_map else -1

    result: dict[str, int | float] = {}

    for obj, metric in pairs:
        rows = _query(conn, "stop", obj, metric)
        if not rows:
            continue

        base = f"{obj}.{metric}"

        if len(rows) == 1:
            result[base] = rows[0][1]
            continue

        # 複数行: per-index + 集計
        core_ids = sorted(c for c, _ in rows)
        is_sim_cores = cpu_map and all(c <= max_sim_core for c in core_ids)

        grand = 0
        node_sum: dict[int, int] = {0: 0, 1: 0}

        for core, val in rows:
            result[f"{base}.c{core}"] = val
            grand += val
            if is_sim_cores:
                node = core_to_node.get(core, 0)
            else:
                # DRAM コントローラ等: ソート順で node0/node1 割り当て
                node = core_ids.index(core)
            node_sum[min(node, 1)] = node_sum.get(min(node, 1), 0) + val

        result[f"{base}.total"] = grand
        result[f"{base}.node0"] = node_sum.get(0, 0)
        result[f"{base}.node1"] = node_sum.get(1, 0)

    conn.close()
    return result


# ─── サマリー表示 ────────────────────────────────────────────────

def parse_sim_seconds(output_dir: str) -> float | None:
    """互換用。parse_sim_time() の別名。"""
    return parse_sim_time(output_dir)


def print_summary(
    node_stats: dict,
    cpu_map: list,
    num_threads: int,
    output_dir: str = "",
) -> None:
    """NUMA アクセス集計をターミナルに表示する。"""
    if not node_stats:
        return

    total_r = sum(v["reads"]  for v in node_stats.values())
    total_w = sum(v["writes"] for v in node_stats.values())
    grand = total_r + total_w

    cpu_ranges = {0: "CPU  0- 7", 1: "CPU  8-15"}

    print("\n" + "=" * 62)
    print("  NUMA メモリアクセス集計 (DRAMコントローラ別)")
    print("=" * 62)
    for n, stats in node_stats.items():
        total = stats["reads"] + stats["writes"]
        ratio = total / grand * 100 if grand > 0 else 0
        print(f"  Node {n} ({cpu_ranges.get(n, f'Node{n}')}):")
        print(f"    reads  = {stats['reads']:>14,}")
        print(f"    writes = {stats['writes']:>14,}")
        print(f"    合計   = {total:>14,}  ({ratio:.1f}%)")
    print("-" * 62)
    print(f"  全体合計: reads={total_r:,}  writes={total_w:,}")

    # L1-D local/remote アクセス (真のNUMA距離統計)
    if output_dir:
        numa = parse_numa_access(output_dir)
        loc, rem = numa["local"], numa["remote"]
        tot = loc + rem
        if tot > 0:
            print(f"\n  L1-D DRAMアクセス (コアから見た距離):")
            print(f"    ローカル (local)  = {loc:>10,}  ({loc/tot*100:.1f}%)")
            print(f"    リモート (remote) = {rem:>10,}  ({rem/tot*100:.1f}%)")

    print("\n  スレッド → CPU → ノード マッピング:")
    for t in range(min(num_threads, 16)):
        cpu = cpu_map[t]
        node  = 0 if cpu in NODE0_CPUS else 1
        ctype = "P" if cpu in P_CORES else "E"
        print(f"    thread {t:2d} → CPU {cpu:2d}  (Node {node}, {ctype}-core)")
    print("=" * 62)
