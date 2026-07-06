"""
result_cache.py
同一cpu_mapの実験結果が既に存在する場合、Sniperを再実行せず既存の出力ディレクトリを
複製・リネームして使い回すための汎用ユーティリティ。

Sniperはcpu_mapに対して決定論的なシミュレータなので、cpu_mapが完全一致する限り
結果も完全に同一になる（科学的に正当な複製）。以前 orchestrator.py にあった
_materialize_mpo_copy はMPOと他戦略の一致判定に限定されていたが、これはラベル名を
問わず「任意の2つの結果がcpu_map一致するか」を汎用的に扱う。

想定用途:
  - AKARINの候補生成(alpha掃引・階層順序の候補)で、低スレッド数ほど配置パターンの
    重複が激しいため、既存結果(候補内・既存の5戦略実測)との一致を確認し複製で済ませる
  - ultra_orchestrator.py の通常ジョブでも、2つの戦略のcpu_mapがたまたま完全一致する
    場合に同様に適用可能
"""

import ast
import glob
import os
import shutil


def find_existing_output(output_base: str, num_threads: int, workload: str,
                         bench_class: str, cpu_map: list) -> tuple[str, str] | None:
    """
    Outputs/size{cls}/{N}TH/ 配下から、workload・スレッド数が一致し、かつ
    affinity_config.txt の cpu_map が完全一致する既存ディレクトリを探す
    (直近のタイムスタンプを優先)。見つかれば (ディレクトリパス, そのPRESET名) を返す。
    無ければ None。
    """
    thread_dir = os.path.join(output_base, f"{num_threads}TH")
    pattern = os.path.join(thread_dir, f"{workload}_{bench_class}_*_{num_threads}TH_*")
    matches = sorted(glob.glob(pattern), reverse=True)
    for d in matches:
        ac_path = os.path.join(d, "affinity_config.txt")
        if not os.path.exists(ac_path):
            continue
        existing_map = None
        preset = None
        with open(ac_path) as f:
            for line in f:
                if line.startswith("cpu_map="):
                    try:
                        existing_map = ast.literal_eval(line.strip().split("=", 1)[1])
                    except (ValueError, SyntaxError):
                        existing_map = None
                elif line.startswith("PRESET="):
                    preset = line.strip().split("=", 1)[1]
        if existing_map == list(cpu_map):
            return d, preset
    return None


def materialize_copy(src_dir: str, new_label: str, workload: str, bench_class: str,
                     num_threads: int, source_label: str, output_base: str,
                     run_id: str) -> str:
    """
    src_dir (source_label の結果) を丸ごと複製し、new_label 名義の出力ディレクトリとして
    生成する。cpu_map一致さえ確認済みであれば、複製結果は実行結果として科学的に正当。
    """
    new_dir = os.path.join(
        output_base, f"{num_threads}TH",
        f"{workload}_{bench_class}_{new_label}_{num_threads}TH_{run_id}",
    )
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)
    shutil.copytree(src_dir, new_dir)

    threads_label = f"{num_threads}TH"

    old_cfg = os.path.join(new_dir, f"arrow_lake_{source_label}_{threads_label}.cfg")
    new_cfg = os.path.join(new_dir, f"arrow_lake_{new_label}_{threads_label}.cfg")
    if os.path.exists(old_cfg):
        os.rename(old_cfg, new_cfg)

    ac_path = os.path.join(new_dir, "affinity_config.txt")
    if os.path.exists(ac_path):
        with open(ac_path) as f:
            content = f.read()
        content = content.replace(f"PRESET={source_label}", f"PRESET={new_label}")
        content += (f"\n# NOTE: {source_label}とcpu_mapが完全一致するため複製生成"
                    f"（実シミュレーションはスキップ）\n")
        with open(ac_path, "w") as f:
            f.write(content)

    mc_path = os.path.join(new_dir, "metrics.csv")
    if os.path.exists(mc_path):
        with open(mc_path) as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            if line.startswith("strategy,"):
                line = f"strategy,{new_label}\n"
            elif line.startswith("output_dir,"):
                line = line.replace(f"_{source_label}_", f"_{new_label}_")
            new_lines.append(line)
        with open(mc_path, "w") as f:
            f.writelines(new_lines)

    si_path = os.path.join(new_dir, "sim.info")
    if os.path.exists(si_path):
        with open(si_path) as f:
            content = f.read()
        content = content.replace(f"arrow_lake_{source_label}_{threads_label}.cfg",
                                  f"arrow_lake_{new_label}_{threads_label}.cfg")
        with open(si_path, "w") as f:
            f.write(content)

    print(f"  [複製] {workload}/{new_label}: {source_label}とcpu_map完全一致のため複製"
          f"(実シミュレーションはスキップ)", flush=True)
    return new_dir
