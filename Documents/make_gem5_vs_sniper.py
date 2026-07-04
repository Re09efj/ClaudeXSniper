"""
gem5 vs Sniper wallTime 比較グラフ生成
出力: Documents/figs_gem5_vs_sniper_20260630/
"""

import json
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.colors import LogNorm

# ── データ読み込み ──────────────────────────────────────────
GEM5_PROFILE   = "/home/hiragahama/gem5/ClaudeX/Documents/Data/run_profile.json"
SNIPER_PROFILE = "/home/hiragahama/ClaudeXSniper/Data/run_profile.json"
OUT_DIR        = "/home/hiragahama/ClaudeXSniper/Documents/figs_gem5_vs_sniper_20260630"

os.makedirs(OUT_DIR, exist_ok=True)

with open(GEM5_PROFILE)   as f: gem5   = json.load(f)
with open(SNIPER_PROFILE) as f: sniper = json.load(f)

WORKLOADS = ["BT","CG","FT","IS","MG","SP","lavaMD","BFS","PR","BC","CC","SSSP","TC"]
THREADS   = [2, 4, 8, 16]

# (wl, th) → (gem5_wt, sniper_wt, speedup) の辞書
data = {}
for wl in WORKLOADS:
    for th in THREADS:
        key = f"{wl}_S_{th}"
        if key in gem5 and key in sniper:
            g = gem5[key]["wallTime"]
            s = sniper[key]["wallTime"]
            data[(wl, th)] = (g, s, g / s)

print(f"共通データ数: {len(data)} エントリ")

# ── スタイル設定 ──────────────────────────────────────────
PALETTE = {2: "#4C72B0", 4: "#DD8452", 8: "#55A868", 16: "#C44E52"}
TH_LABELS = {2: "2 Thread", 4: "4 Thread", 8: "8 Thread", 16: "16 Thread"}

plt.rcParams.update({
    "font.family":      "DejaVu Sans",
    "axes.spines.top":  False,
    "axes.spines.right":False,
    "axes.grid":        True,
    "grid.alpha":       0.35,
    "grid.linestyle":   "--",
})

# ═══════════════════════════════════════════════════════════
# 図 1: スピードアップ比 ヒートマップ
# ═══════════════════════════════════════════════════════════
speedup_mat = np.zeros((len(WORKLOADS), len(THREADS)))
for i, wl in enumerate(WORKLOADS):
    for j, th in enumerate(THREADS):
        if (wl, th) in data:
            speedup_mat[i, j] = data[(wl, th)][2]

fig, ax = plt.subplots(figsize=(8, 6))
im = ax.imshow(speedup_mat, cmap="YlOrRd", aspect="auto",
               norm=LogNorm(vmin=1, vmax=speedup_mat.max()))

ax.set_xticks(range(len(THREADS)))
ax.set_xticklabels([f"{t}TH" for t in THREADS], fontsize=12)
ax.set_yticks(range(len(WORKLOADS)))
ax.set_yticklabels(WORKLOADS, fontsize=11)
ax.set_title("Sniper Speedup over gem5  (Class S, Wall-clock Time)", fontsize=13, pad=12)
ax.set_xlabel("Thread Count", fontsize=11)

for i in range(len(WORKLOADS)):
    for j in range(len(THREADS)):
        v = speedup_mat[i, j]
        if v > 0:
            txt = f"{v:.0f}×" if v >= 10 else f"{v:.1f}×"
            color = "white" if v > 100 else "black"
            ax.text(j, i, txt, ha="center", va="center", fontsize=9, color=color, fontweight="bold")

cbar = fig.colorbar(im, ax=ax, shrink=0.8)
cbar.set_label("Speedup (×)", fontsize=10)
cbar.ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.0f}×"))

plt.tight_layout()
out1 = os.path.join(OUT_DIR, "fig1_speedup_heatmap.png")
fig.savefig(out1, dpi=180, bbox_inches="tight")
plt.close()
print(f"保存: {out1}")

# ═══════════════════════════════════════════════════════════
# 図 2: スピードアップ棒グラフ（スレッド数別 4パネル）
# ═══════════════════════════════════════════════════════════
fig, axes = plt.subplots(2, 2, figsize=(14, 9), sharey=False)
axes = axes.flatten()

for ax_idx, th in enumerate(THREADS):
    ax = axes[ax_idx]
    wls = [wl for wl in WORKLOADS if (wl, th) in data]
    speedups = [data[(wl, th)][2] for wl in wls]
    order = np.argsort(speedups)[::-1]
    wls_s  = [wls[i] for i in order]
    spd_s  = [speedups[i] for i in order]

    bars = ax.bar(range(len(wls_s)), spd_s, color=PALETTE[th], edgecolor="white", linewidth=0.5)
    ax.set_xticks(range(len(wls_s)))
    ax.set_xticklabels(wls_s, rotation=40, ha="right", fontsize=9)
    ax.set_title(f"{th} Threads", fontsize=12, color=PALETTE[th], fontweight="bold")
    ax.set_ylabel("Speedup (×)", fontsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.0f}×"))

    for bar, val in zip(bars, spd_s):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(spd_s) * 0.01,
                f"{val:.0f}×" if val >= 10 else f"{val:.1f}×",
                ha="center", va="bottom", fontsize=8, fontweight="bold")

fig.suptitle("Sniper vs gem5 — Simulation Speedup by Workload  (Class S)",
             fontsize=14, fontweight="bold", y=1.01)
plt.tight_layout()
out2 = os.path.join(OUT_DIR, "fig2_speedup_bars.png")
fig.savefig(out2, dpi=180, bbox_inches="tight")
plt.close()
print(f"保存: {out2}")

# ═══════════════════════════════════════════════════════════
# 図 3: log-log 散布図  gem5 vs Sniper wallTime
# ═══════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(8, 7))

all_gem5   = [v[0] for v in data.values()]
all_sniper = [v[1] for v in data.values()]
lo = min(min(all_gem5), min(all_sniper)) * 0.7
hi = max(max(all_gem5), max(all_sniper)) * 1.5

ax.plot([lo, hi], [lo, hi],    color="gray", lw=1.2, ls="--", label="gem5 = Sniper (1×)")
ax.plot([lo, hi], [lo/10, hi/10], color="#aaa", lw=0.8, ls=":", label="10× faster")
ax.plot([lo, hi], [lo/100, hi/100], color="#ccc", lw=0.8, ls=":", label="100× faster")

for th in THREADS:
    xs = [data[(wl, th)][0] for wl in WORKLOADS if (wl, th) in data]
    ys = [data[(wl, th)][1] for wl in WORKLOADS if (wl, th) in data]
    wl_names = [wl for wl in WORKLOADS if (wl, th) in data]
    ax.scatter(xs, ys, color=PALETTE[th], s=70, zorder=3,
               label=f"{th} Threads", edgecolors="white", linewidths=0.5)
    for x, y, wl in zip(xs, ys, wl_names):
        ratio = x / y
        if ratio > 200 or ratio < 3:
            ax.annotate(wl, (x, y), textcoords="offset points",
                        xytext=(4, 4), fontsize=7, color=PALETTE[th])

ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xlabel("gem5 Wall-clock Time (s)", fontsize=11)
ax.set_ylabel("Sniper Wall-clock Time (s)", fontsize=11)
ax.set_title("gem5 vs Sniper: Wall-clock Time  (Class S, log scale)", fontsize=13, pad=10)
ax.legend(fontsize=9, loc="upper left")

ax.text(0.97, 0.04, "← Sniper faster", transform=ax.transAxes,
        ha="right", fontsize=9, color="gray", style="italic")

ax.set_xlim(lo, hi)
ax.set_ylim(lo / 1500, hi)

out3 = os.path.join(OUT_DIR, "fig3_scatter_loglog.png")
plt.tight_layout()
fig.savefig(out3, dpi=180, bbox_inches="tight")
plt.close()
print(f"保存: {out3}")

# ═══════════════════════════════════════════════════════════
# 図 4: スレッド数別 平均スピードアップ（横棒＋分布）
# ═══════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(8, 4.5))

means, medians, q1s, q3s = [], [], [], []
for th in THREADS:
    spds = [data[(wl, th)][2] for wl in WORKLOADS if (wl, th) in data]
    means.append(np.mean(spds))
    medians.append(np.median(spds))
    q1s.append(np.percentile(spds, 25))
    q3s.append(np.percentile(spds, 75))

x = np.arange(len(THREADS))
bars = ax.bar(x, means, color=[PALETTE[t] for t in THREADS],
              edgecolor="white", linewidth=0.5, width=0.55, label="Mean speedup")
ax.scatter(x, medians, marker="D", color="white", edgecolors="black",
           s=55, zorder=4, label="Median")

for xi, (m, q1, q3) in enumerate(zip(means, q1s, q3s)):
    ax.vlines(xi, q1, q3, colors="black", linewidth=2.5, zorder=5)
    ax.text(xi, m + max(means) * 0.02, f"{m:.0f}×",
            ha="center", va="bottom", fontsize=11, fontweight="bold")

ax.set_xticks(x)
ax.set_xticklabels([f"{t} Threads" for t in THREADS], fontsize=11)
ax.set_ylabel("Speedup over gem5 (×)", fontsize=11)
ax.set_title("Average Simulation Speedup: Sniper vs gem5  (Class S, 13 workloads)",
             fontsize=13, pad=10)
ax.legend(fontsize=9)
ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.0f}×"))

out4 = os.path.join(OUT_DIR, "fig4_mean_speedup.png")
plt.tight_layout()
fig.savefig(out4, dpi=180, bbox_inches="tight")
plt.close()
print(f"保存: {out4}")

# ── テキストサマリー ──────────────────────────────────────
print("\n=== スピードアップ統計 ===")
all_spds = [v[2] for v in data.values()]
print(f"全体: 平均 {np.mean(all_spds):.1f}×  中央値 {np.median(all_spds):.1f}×  最大 {max(all_spds):.0f}×  最小 {min(all_spds):.1f}×")
for th in THREADS:
    spds = [data[(wl, th)][2] for wl in WORKLOADS if (wl, th) in data]
    wl_max = max(WORKLOADS, key=lambda w: data.get((w, th), (0,0,0))[2])
    print(f"  {th}TH: 平均 {np.mean(spds):.1f}×  最大 {max(spds):.0f}× ({wl_max})")
print(f"\nグラフ保存先: {OUT_DIR}")
