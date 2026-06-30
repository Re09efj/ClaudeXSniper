"""
power_model.py
Sniper シミュレーション統計から解析的に電力・エネルギーを推定する。

注: Sniper コンテナに McPAT バイナリが含まれないため、
    sim.stats.sqlite3 の IPC・サイクル統計から近似計算する。

モデル:
  P-core (4GHz, OOO): 動的電力 8W@peak + リーク 0.5W
  E-core (1GHz, in-order): 動的電力 1.5W@peak + リーク 0.2W
  アクティビティ係数 = IPC / peak_IPC
  DRAM エネルギー: reads×64B×0.1nJ + writes×64B×0.07nJ

単体実行:
  python3 -m utility.power_model <output_dir>
"""

import json
import os
import sys

from utility.stats_reader import (
    parse_sim_time,
    parse_ipc,
    parse_instructions,
    parse_cycles,
    parse_node_stats,
    P_CORES,
    E_CORES,
)

_P = dict(
    p_core_dynamic_W    = 8.0,
    p_core_leakage_W    = 0.5,
    e_core_dynamic_W    = 1.5,
    e_core_leakage_W    = 0.2,
    dram_read_energy_J  = 6.4e-9,   # 64B × 0.1 nJ/byte
    dram_write_energy_J = 4.5e-9,   # 64B × 0.07 nJ/byte
)


def estimate(output_dir: str, cpu_map: list | None = None, num_threads: int = 16) -> dict:
    sim_seconds = parse_sim_time(output_dir)
    if not sim_seconds:
        return {}

    ipc_map    = parse_ipc(output_dir, cpu_map)
    inst_map   = parse_instructions(output_dir)
    node_stats = parse_node_stats(output_dir)

    dynamic_W = 0.0
    leakage_W = 0.0

    for sim_core in range(num_threads):
        cpu_id = cpu_map[sim_core] if cpu_map and sim_core < len(cpu_map) else sim_core
        insts  = inst_map.get(sim_core, 0)
        ipc    = ipc_map.get(sim_core, 0)
        if insts == 0:
            continue

        if cpu_id in P_CORES:
            tdp_W, leak_W = _P["p_core_dynamic_W"], _P["p_core_leakage_W"]
        else:
            tdp_W, leak_W = _P["e_core_dynamic_W"], _P["e_core_leakage_W"]

        activity   = min(ipc / 4.0, 1.0) if ipc > 0 else 0.5
        dynamic_W += tdp_W * activity
        leakage_W += leak_W

    total_reads  = sum(v["reads"]  for v in node_stats.values())
    total_writes = sum(v["writes"] for v in node_stats.values())
    dram_energy_J = (total_reads  * _P["dram_read_energy_J"] +
                     total_writes * _P["dram_write_energy_J"])
    dram_W   = dram_energy_J / sim_seconds
    total_W  = dynamic_W + leakage_W + dram_W
    energy_J = total_W * sim_seconds

    return {
        "dynamic_W":   round(dynamic_W, 4),
        "leakage_W":   round(leakage_W, 4),
        "dram_W":      round(dram_W, 4),
        "total_W":     round(total_W, 4),
        "energy_J":    round(energy_J, 6),
        "sim_seconds": round(sim_seconds, 6),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m utility.power_model <output_dir>")
        sys.exit(1)
    result = estimate(sys.argv[1], num_threads=int(sys.argv[2]) if len(sys.argv) > 2 else 16)
    print(json.dumps(result, indent=2))
