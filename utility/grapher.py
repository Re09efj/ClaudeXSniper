"""
grapher.py
Sniper シミュレーション結果から4種類のグラフを生成する。
  1. generate_core_stats_graph  - IPC / Instructions per core
  2. generate_numa_graph        - 単一実験の Node0/Node1 アクセス比較
  3. generate_latency_graph     - L1-D loads-where-* メモリ階層内訳
  4. generate_comparison_graph  - 複数ワークロード × 戦略 横断比較（Jin Fig.4.7 スタイル）
"""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from utility.stats_reader import (
    parse_ipc,
    parse_instructions,
    parse_cycles,
    parse_l1d_where,
    P_CORES,
    NODE0_CPUS,
)


# ─────────────────────────────────────────────
# 1. IPC / Instructions per core
# ─────────────────────────────────────────────
def generate_core_stats_graph(
    output_dir: str,
    cpu_map: list,
    num_threads: int,
    num_nodes: int = 2,
    big_cpn: int = 4,
    sml_cpn: int = 4,
) -> None:
    ipc_map   = parse_ipc(output_dir, cpu_map)
    inst_map  = parse_instructions(output_dir)

    if not ipc_map and not inst_map:
        print(f"[grapher] コア統計が見つかりません: {output_dir}")
        return

    ipc_data, inst_data = [], []
    for sim_core in range(num_threads):
        cpu_id = cpu_map[sim_core] if sim_core < len(cpu_map) else sim_core
        node   = 0 if cpu_id in NODE0_CPUS else 1
        ctype  = "P-core" if cpu_id in P_CORES else "E-core"
        label  = f"C{cpu_id}\n(N{node},{ctype[0]})"

        ipc = ipc_map.get(sim_core, 0)
        if ipc and ipc > 0:
            ipc_data.append({"Core": label, "IPC": ipc, "CoreType": ctype})

        insts = inst_map.get(sim_core, 0)
        if insts and insts > 0:
            inst_data.append({"Core": label, "Instructions": insts, "CoreType": ctype})

    if not ipc_data and not inst_data:
        print("[grapher] 警告: コア統計データがありません。")
        return

    type_palette = {"P-core": "steelblue", "E-core": "tomato"}
    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(1, 2, figsize=(18, 6))

    if ipc_data:
        df_ipc = pd.DataFrame(ipc_data)
        sns.barplot(x="Core", y="IPC", data=df_ipc, hue="CoreType",
                    palette=type_palette, ax=axes[0], legend=True, dodge=False)
        axes[0].set_title("IPC per Core (Higher is Better)", fontsize=14, fontweight="bold")
        axes[0].set_xlabel("CPU Cores", fontsize=12)
        axes[0].set_ylabel("IPC (Instructions Per Cycle)", fontsize=12)
        axes[0].tick_params(axis="x", rotation=45)
        for p in axes[0].patches:
            if p.get_height() > 0:
                axes[0].annotate(
                    f"{p.get_height():.2f}",
                    (p.get_x() + p.get_width() / 2.0, p.get_height()),
                    ha="center", va="bottom", xytext=(0, 4),
                    textcoords="offset points", fontsize=8,
                )
    else:
        axes[0].text(0.5, 0.5, "IPC data not available", ha="center", va="center")

    if inst_data:
        df_inst = pd.DataFrame(inst_data)
        sns.barplot(x="Core", y="Instructions", data=df_inst, hue="CoreType",
                    palette=type_palette, ax=axes[1], legend=True, dodge=False)
        axes[1].set_title("Instructions per Core", fontsize=14, fontweight="bold")
        axes[1].set_xlabel("CPU Cores", fontsize=12)
        axes[1].set_ylabel("Instruction Count", fontsize=12)
        axes[1].tick_params(axis="x", rotation=45)
    else:
        axes[1].text(0.5, 0.5, "Instruction data not available", ha="center", va="center")

    plt.tight_layout()
    out = os.path.join(output_dir, "core_stats_summary.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[grapher] core_stats_summary.png 保存: {out}")


# ─────────────────────────────────────────────
# 2. 単一実験 NUMA アクセス比較
# ─────────────────────────────────────────────
def generate_numa_graph(
    output_dir: str,
    node_stats: dict,
    preset_name: str,
    cpu_map: list,
    num_threads: int,
    workload: str = "IS",
    bench_class: str = "?",
) -> None:
    if not node_stats:
        return

    nodes  = list(node_stats.keys())
    reads  = [node_stats[n]["reads"]  for n in nodes]
    writes = [node_stats[n]["writes"] for n in nodes]
    totals = [reads[i] + writes[i]    for i in range(len(nodes))]
    labels = [f"Node {n}\n(CPU {'0-7' if n == 0 else '8-15'})" for n in nodes]

    node_thread_count = {0: 0, 1: 0}
    for t in range(min(num_threads, 16)):
        node_thread_count[0 if cpu_map[t] in NODE0_CPUS else 1] += 1

    colors_read  = ["#4C9BE8", "#E87A4C"]
    colors_write = ["#A8C8F0", "#F0BCA8"]

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(
        f"NUMA Memory Access — {workload.upper()} Class {bench_class}  "
        f"[preset: {preset_name}, {num_threads} threads]",
        fontsize=13, fontweight="bold",
    )

    x = range(len(nodes))
    bars_r = axes[0].bar(x, reads,  color=colors_read,  label="Reads",  width=0.5)
    bars_w = axes[0].bar(x, writes, color=colors_write, label="Writes", bottom=reads, width=0.5)
    axes[0].set_xticks(list(x))
    axes[0].set_xticklabels(labels, fontsize=11)
    axes[0].set_ylabel("Memory Requests", fontsize=11)
    axes[0].set_title("Reads & Writes per Node", fontsize=12)
    axes[0].legend()
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    for bar, val in zip(bars_r, reads):
        if val > 0:
            axes[0].text(bar.get_x() + bar.get_width() / 2, val / 2,
                         f"{val:,}", ha="center", va="center",
                         fontsize=9, color="white", fontweight="bold")
    for bar, r_val, w_val in zip(bars_w, reads, writes):
        if w_val > 0:
            axes[0].text(bar.get_x() + bar.get_width() / 2, r_val + w_val / 2,
                         f"{w_val:,}", ha="center", va="center",
                         fontsize=9, color="white", fontweight="bold")

    grand = sum(totals)
    if grand > 0:
        pie_labels = [
            f"Node {n}\n{totals[n]:,} reqs\n({node_thread_count[n]} threads)"
            for n in nodes
        ]
        axes[1].pie(totals, labels=pie_labels,
                    colors=["#4C9BE8", "#E87A4C"],
                    autopct="%1.1f%%", startangle=90,
                    textprops={"fontsize": 10},
                    wedgeprops={"edgecolor": "white", "linewidth": 2})
        axes[1].set_title("Total Access Share per Node", fontsize=12)
    else:
        axes[1].text(0.5, 0.5, "No data", ha="center", va="center")

    mapping_str = (
        "Thread→CPU: "
        + ", ".join(
            f"T{t}→C{cpu_map[t]}(N{0 if cpu_map[t] in NODE0_CPUS else 1})"
            for t in range(min(num_threads, 8))
        )
        + (" ..." if num_threads > 8 else "")
    )
    fig.text(0.5, 0.01, mapping_str, ha="center", fontsize=8, color="gray")

    plt.tight_layout(rect=[0, 0.04, 1, 1])
    out = os.path.join(output_dir, "numa_access.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[grapher] numa_access.png 保存: {out}")


# ─────────────────────────────────────────────
# 3. L1-D loads-where-* メモリ階層内訳
# ─────────────────────────────────────────────
def generate_latency_graph(
    output_dir: str,
    cpu_map: list,
    num_threads: int,
    num_nodes: int = 2,
    big_cpn: int = 4,
    sml_cpn: int = 4,
) -> None:
    """
    Sniper の L1-D loads-where-* でメモリ階層別アクセス内訳を可視化する。
    Subplot 1: コア別 ローカル vs リモート DRAM アクセス比
    Subplot 2: 全体 キャッシュ階層内訳（L2/L3/DRAM-local/DRAM-remote）
    Subplot 3: ノード別 DRAM リモートアクセス率
    """
    per_core = parse_l1d_where(output_dir)
    if not per_core:
        print(f"[grapher] L1-D 統計が見つかりません: {output_dir}")
        return

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(1, 3, figsize=(20, 6))
    fig.suptitle("Memory Hierarchy Access Breakdown (L1-D loads-where-*)",
                 fontsize=14, fontweight="bold")

    # ── Subplot 1: コア別 local vs remote DRAM ──
    core_data = []
    for sim_core in range(num_threads):
        d = per_core.get(sim_core, {})
        cpu_id = cpu_map[sim_core] if sim_core < len(cpu_map) else sim_core
        node   = 0 if cpu_id in NODE0_CPUS else 1
        ctype  = "P" if cpu_id in P_CORES else "E"
        core_data.append({
            "label":  f"C{cpu_id}\n(N{node},{ctype})",
            "local":  d.get("dram_local", 0),
            "remote": d.get("dram_remote", 0),
            "node":   node,
        })

    if any(d["local"] + d["remote"] > 0 for d in core_data):
        xlabels = [d["label"] for d in core_data]
        locals_ = [d["local"]  for d in core_data]
        remotes = [d["remote"] for d in core_data]
        x = range(len(core_data))
        axes[0].bar(x, locals_,  color="#4C9BE8", label="Local DRAM",  width=0.6)
        axes[0].bar(x, remotes, color="#E87A4C", label="Remote DRAM",
                    bottom=locals_, width=0.6)
        axes[0].set_xticks(list(x))
        axes[0].set_xticklabels(xlabels, fontsize=8)
        axes[0].set_ylabel("Load Count", fontsize=11)
        axes[0].set_title("DRAM Local vs Remote per Core", fontsize=12)
        axes[0].legend(fontsize=9)
    else:
        axes[0].text(0.5, 0.5, "No DRAM access data", ha="center", va="center")

    # ── Subplot 2: 全体 キャッシュ階層内訳 ──
    totals = {"L2": 0, "L3": 0, "DRAM-Local": 0, "DRAM-Remote": 0}
    for d in per_core.values():
        totals["L2"]          += d.get("l2", 0)
        totals["L3"]          += d.get("l3", 0)
        totals["DRAM-Local"]  += d.get("dram_local", 0)
        totals["DRAM-Remote"] += d.get("dram_remote", 0)

    grand = sum(totals.values())
    if grand > 0:
        colors = ["#5BA85A", "#F0C040", "#4C9BE8", "#E87A4C"]
        axes[1].pie(
            [totals[k] for k in totals],
            labels=[f"{k}\n{totals[k]:,}" for k in totals],
            colors=colors, autopct="%1.1f%%", startangle=90,
            textprops={"fontsize": 9},
            wedgeprops={"edgecolor": "white", "linewidth": 2},
        )
        axes[1].set_title("Total Cache Hierarchy Breakdown", fontsize=12)
    else:
        axes[1].text(0.5, 0.5, "No cache hierarchy data", ha="center", va="center")

    # ── Subplot 3: ノード別 リモートアクセス率 ──
    node_remote = {0: 0, 1: 0}
    node_total  = {0: 0, 1: 0}
    for sim_core in range(num_threads):
        d = per_core.get(sim_core, {})
        cpu_id = cpu_map[sim_core] if sim_core < len(cpu_map) else sim_core
        node   = 0 if cpu_id in NODE0_CPUS else 1
        node_remote[node] += d.get("dram_remote", 0)
        node_total[node]  += d.get("dram_local", 0) + d.get("dram_remote", 0)

    node_colors = ["steelblue", "tomato"]
    remote_rates = [
        node_remote[n] / node_total[n] * 100 if node_total[n] > 0 else 0
        for n in range(num_nodes)
    ]
    local_rates = [100 - r for r in remote_rates]
    x = range(num_nodes)
    axes[2].bar(x, local_rates,  color=["#A8D8A8", "#F0BCA8"],
                label="Local", width=0.5)
    axes[2].bar(x, remote_rates, color=["#4C9BE8", "#E87A4C"],
                label="Remote", bottom=local_rates, width=0.5)
    axes[2].set_xticks(list(x))
    axes[2].set_xticklabels([f"Node {n}" for n in range(num_nodes)], fontsize=11)
    axes[2].set_ylabel("DRAM Access Rate (%)", fontsize=11)
    axes[2].set_ylim(0, 115)
    axes[2].set_title("Remote DRAM Access Rate per Node", fontsize=12)
    axes[2].legend(fontsize=9)
    for i, r in enumerate(remote_rates):
        if r > 0:
            axes[2].text(i, local_rates[i] + r / 2,
                         f"{r:.1f}%", ha="center", va="center",
                         fontsize=11, color="white", fontweight="bold")

    plt.tight_layout()
    out = os.path.join(output_dir, "latency_breakdown.png")
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"[grapher] latency_breakdown.png 保存: {out}")


# ─────────────────────────────────────────────
# 4. 複数ワークロード × 戦略 横断比較（Jin Fig.4.7 スタイル）
# ─────────────────────────────────────────────
def generate_comparison_graph(
    groups: list[tuple[str, dict[str, float]]],
    baseline_label: str,
    proposal_label: str,
    output_path: str,
    title: str = "Performance improvements from memory-aware mapping optimization",
) -> None:
    """Jin の Figure 4.7 スタイルの正規化比較グラフを生成する。

    Args:
        groups: [(group_label, {strategy_label: value}), ...]
        baseline_label: 正規化基準の戦略名（この戦略 = 1.0）
        proposal_label: 提案手法の戦略名
        output_path: 出力 PNG のパス
        title: グラフタイトル
    """
    n_groups = len(groups)
    x = range(n_groups)
    width = 0.35

    baseline_vals, proposal_vals, xlabels = [], [], []
    for label, vals in groups:
        base = vals.get(baseline_label, 0) or 0
        prop = vals.get(proposal_label, 0) or 0
        norm_base = 1.0
        norm_prop = prop / base if base != 0 else float("nan")
        baseline_vals.append(norm_base)
        proposal_vals.append(norm_prop)
        xlabels.append(label)

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(10, 5))

    bars_b = ax.bar(
        [i - width / 2 for i in x], baseline_vals, width=width,
        label=baseline_label, color="#C0692A", zorder=3,
    )
    bars_p = ax.bar(
        [i + width / 2 for i in x], proposal_vals, width=width,
        label=proposal_label, color="#F0C040", zorder=3,
    )

    ax.set_xticks(list(x))
    ax.set_xticklabels(xlabels, fontsize=11)
    ax.set_ylabel("Normalized performance metrics", fontsize=12)
    valid_props = [v for v in proposal_vals if v == v]
    ymax = max(max(baseline_vals), max(valid_props)) * 1.25 if valid_props else 1.5
    ax.set_ylim(0, ymax)
    ax.axhline(1.0, color="gray", linestyle="--", linewidth=0.8, zorder=2)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=11)

    for bar, val in zip(bars_b, baseline_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.01,
                f"{val:.2f}", ha="center", va="bottom", fontsize=9)
    for bar, val in zip(bars_p, proposal_vals):
        if val == val:
            ax.text(bar.get_x() + bar.get_width() / 2, val + 0.01,
                    f"{val:.2f}", ha="center", va="bottom", fontsize=9)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"[grapher] 比較グラフ保存: {output_path}")
