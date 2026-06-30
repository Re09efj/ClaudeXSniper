"""
sniper_sim.py
podman を使って Sniper コンテナ上でシミュレーションを実行するラッパー。

コンテナ: snipersim/snipersim:latest
  - /root/sniper/run-sniper がエントリポイント
  - バイナリは --binary でホストパスを渡し、コンテナに /binary としてマウント
  - 出力は /out にマウント
"""

import os
import subprocess


CONTAINER_IMAGE = "snipersim/snipersim:latest"
SNIPER_BIN      = "/root/sniper/run-sniper"
CONTAINER_CFG   = "/cfg/arrow_lake.cfg"
CONTAINER_BIN   = "/binary"
CONTAINER_OUT   = "/out"


def run_sniper(
    binary_path:    str,
    binary_args:    str,
    num_threads:    int,
    cpu_map:        list,
    strategy:       str,
    output_dir:     str,
    config_path:    str,
    log_file,                   # open file object (writable)
    workload:       str = "",
    omp_num_threads: int | None = None,
    proc_holder:    list | None = None,
) -> int:
    """
    podman run で Sniper シミュレーションを実行する。

    Parameters
    ----------
    binary_path   : ホスト上のバイナリ絶対パス
    binary_args   : バイナリへの引数文字列 (スペース区切り)
    num_threads   : シミュレートするコア数
    cpu_map       : スレッド→CPU マッピングリスト (アフィニティ設定用)
    strategy      : 戦略名 (ログ用)
    output_dir    : ホスト上の出力ディレクトリ
    config_path   : ホスト上の Sniper 設定ファイルパス
    log_file      : ログ書き込み先 (file object)
    omp_num_threads: OMP_NUM_THREADS (None の場合 num_threads を使用)

    Returns
    -------
    int: subprocess の return code (0 = 成功)
    """
    os.makedirs(output_dir, exist_ok=True)

    n_omp = omp_num_threads if omp_num_threads is not None else num_threads

    binary_dir  = os.path.dirname(binary_path)
    binary_name = os.path.basename(binary_path)
    cfg_dir     = os.path.dirname(config_path)
    cfg_name    = os.path.basename(config_path)

    # GOMP_CPU_AFFINITY: スレッド i を cpu_map[i] にバインド (ヒント)
    gomp_affinity = " ".join(str(cpu_map[i]) for i in range(n_omp))

    podman_cmd = [
        "podman", "run", "--rm",
        "-v", f"{binary_dir}:/binary:ro,z",
        "-v", f"{output_dir}:/out:z",
        "-v", f"{cfg_dir}:/cfg:ro,z",
        "-e", f"OMP_NUM_THREADS={n_omp}",
        "-e", f"GOMP_CPU_AFFINITY={gomp_affinity}",
        CONTAINER_IMAGE,
        SNIPER_BIN,
        "-n", str(num_threads),
        "-d", CONTAINER_OUT,
        "-c", f"/cfg/{cfg_name}",
        "--",
        f"{CONTAINER_BIN}/{binary_name}",
    ]

    # バイナリ引数を追加
    if binary_args:
        podman_cmd.extend(binary_args.split())

    proc = subprocess.Popen(podman_cmd, stdout=log_file, stderr=log_file)
    if proc_holder is not None:
        proc_holder.append(proc)
    return proc.wait()
