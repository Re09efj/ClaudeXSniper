"""
generate_candidates.py
AKARIN訓練データ生成の第一段階: 「筋の通った」候補cpu_mapを算定するだけのスクリプト。
Sniperは一切実行しない(CP-SATの計算のみ、高速・低負荷)。進行中のSniperバッチとは
競合しない。

各(workload, bench_class, num_threads)について:
  1. CP-SAT(akarin/cpsat_mapper.py、ルーフラインモデル)で候補cpu_mapを1つ
     計算する。2026-07-11: 以前はalphaを0〜1で振って候補cpu_mapを21点(密)/
     3点(粗)グリッド生成していたが、新数式はalphaという恣意的パラメータを
     持たない決定的な最適化になったため、候補は1点のみになった。
  2. 既存の5戦略(Packed/Scatter/HPO/EPO/MPO)のcpu_mapも候補に加える
  3. 「どのスレッドが(ノード, P/E種別)のどのバケツに乗るか」を正規化した
     署名(canonical signature)をキーに重複排除する。generate_config.pyの
     周波数・キャッシュサイズはP/E種別とノード番号のみで決まり、同じ種別
     バケツ内でのコア番号違い(例: cpu_map=[0,1,2,3,...] と [1,0,3,2,...])は
     Sniperの設定・結果に一切影響しないため、これは厳密に妥当な同値類。

使い方:
  python3 -m akarin.generate_candidates --workload BT --bench-class S --threads 8
  python3 -m akarin.generate_candidates --workload BT BFS lavaMD --bench-class S --threads 2 8 16
"""

import argparse

from akarin.cpsat_mapper import compute_cpsat_map_from_csv
from utility.cpu_affinity import resolve_cpu_map
from utility.deloc_mapper import NODE_E_CORES, NODE_P_CORES, find_comm_csv
from utility.capacity_model import HEAVY_WORKLOADS

LEGACY_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO"]

_CORE_KIND: dict[int, tuple[int, str]] = {}
for _node, _cores in NODE_P_CORES.items():
    for _c in _cores:
        _CORE_KIND[_c] = (_node, "P")
for _node, _cores in NODE_E_CORES.items():
    for _c in _cores:
        _CORE_KIND[_c] = (_node, "E")


def canonical_signature(cpu_map: list) -> tuple:
    """
    cpu_mapを「各スレッドがどの(ノード, P/E種別)バケツに乗るか」の列に正規化する。
    このバケツ構成が一致すれば、同じ種別内でコア番号が違ってもSniperの
    シミュレーション結果は同一になる。
    """
    return tuple(_CORE_KIND[c] for c in cpu_map)


def generate_candidates(workload: str, bench_class: str, num_threads: int) -> dict[tuple, dict]:
    """
    正規化署名 -> {"cpu_map": 代表cpu_map, "labels": [ラベル...]} の辞書を返す。
    同じ署名(=同一シミュレーション結果になる配置)に複数のラベル(AKARINや戦略名)が
    対応する場合はまとめて記録する。
    """
    candidates: dict[tuple, dict] = {}
    csv_path = find_comm_csv(workload, bench_class, num_threads)

    def _add(cpu_map: list, label: str):
        key = canonical_signature(cpu_map[:num_threads])
        entry = candidates.setdefault(key, {"cpu_map": cpu_map, "labels": []})
        entry["labels"].append(label)

    cpu_map, _info, _imbalance = compute_cpsat_map_from_csv(
        csv_path, num_threads, time_limit_sec=5.0,
    )
    _add(cpu_map, "AKARIN")

    for strategy in LEGACY_STRATEGIES:
        cpu_map = resolve_cpu_map(strategy, workload, bench_class, num_threads)
        _add(cpu_map, strategy)

    return candidates


def _parse_args():
    p = argparse.ArgumentParser(description="AKARIN候補cpu_mapの算定(Sniper実行なし)")
    p.add_argument("--workload", nargs="+", required=True)
    p.add_argument("--bench-class", default="S")
    p.add_argument("--threads", nargs="+", type=int, required=True)
    return p.parse_args()


def main():
    args = _parse_args()

    for wl in args.workload:
        for th in args.threads:
            candidates = generate_candidates(wl, args.bench_class, th)
            n_unique = len(candidates)
            tier = "重量級" if wl in HEAVY_WORKLOADS else "軽量級"
            print(f"\n=== {wl} class={args.bench_class} {th}TH [{tier}] ===")
            print(f"  AKARIN(1点、alpha廃止) + 既存5戦略 → ユニーク候補数={n_unique}")
            for entry in sorted(candidates.values(), key=lambda e: -len(e["labels"])):
                labels = entry["labels"]
                label_str = ", ".join(labels) if len(labels) <= 6 else f"{len(labels)}種"
                print(f"    {label_str:<40}  cpu_map={entry['cpu_map']}")


if __name__ == "__main__":
    main()
