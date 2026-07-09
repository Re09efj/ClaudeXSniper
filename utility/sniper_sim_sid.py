"""
sniper_sim_sid.py
podman を使って Sniper コンテナ上でシミュレーションを実行するラッパー(SIDホスト用)。

コンテナ: snipersim/snipersim:latest
  - /root/sniper/run-sniper がエントリポイント
  - バイナリは --binary でホストパスを渡し、コンテナに /binary としてマウント
  - 出力は /out にマウント
"""

import os
import subprocess
import uuid


CONTAINER_IMAGE = "snipersim/snipersim:latest"
SNIPER_BIN      = "/root/sniper/run-sniper"
CONTAINER_CFG   = "/cfg/arrow_lake.cfg"
CONTAINER_BIN   = "/binary"
CONTAINER_OUT   = "/out"


class _LocalPodmanProc:
    """
    proc_holderに積むラッパー。.kill()した際に、ローカルのpodman runクライアント
    プロセスだけでなく、podman kill/rmでコンテナ自体も明示的に停止・削除する。

    背景: 2026-07-09、タイムアウトで`proc.kill()`(ローカルクライアントのみ)を
    実行したはずのBTジョブが、実際には1時間17分もコンテナが動き続けていた事故が
    発生。`podman run`はデタッチしていない前提でもクライアントプロセスへの
    SIGKILLがコンテナ本体(conmonが監督する別プロセスツリー)へ確実に伝播する
    保証が無いため、sniper_sim_purple.pyの_RemoteJobProcと同じ「プロセスグループ/
    コンテナ単位で明示的にkillする」パターンをこちらにも適用する。
    """

    def __init__(self, local_proc: subprocess.Popen, container_name: str):
        self._local = local_proc
        self._container_name = container_name
        self._container_killed = False

    def wait(self) -> int:
        return self._local.wait()

    def kill(self) -> None:
        self._local.kill()
        if self._container_killed:
            return
        self._container_killed = True
        for cmd in (["podman", "kill", self._container_name],
                    ["podman", "rm", "-f", self._container_name]):
            try:
                subprocess.run(cmd, timeout=15,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.TimeoutExpired, OSError):
                pass  # コンテナ停止が失敗/タイムアウトしても呼び出し元には影響させない


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
    stdin_path:     str | None = None,
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
    stdin_path    : 標準入力から読むワークロード(water_nsquared等)用の入力ファイル
                   (ホスト上の絶対パス)。Noneなら通常通り標準入力は繋がない。

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

    # タイムアウト時に確実にkillできるよう、コンテナに一意な名前を付ける
    # (_LocalPodmanProc.kill()参照)。
    container_name = f"sniper_{os.path.basename(output_dir)}_{uuid.uuid4().hex[:8]}"

    podman_cmd = [
        "podman", "run", "--rm", "--name", container_name,
        # 作業ディレクトリをバイナリのマウント先にする。water_nsquared等が
        # カレントディレクトリからの相対パスで補助ファイル(random.in等)を
        # 読むため。SNIPER_BINは絶対パス実行なのでこの変更でも壊れない。
        "-w", CONTAINER_BIN,
        "-v", f"{binary_dir}:/binary:ro,z",
        "-v", f"{output_dir}:/out:z",
        "-v", f"{cfg_dir}:/cfg:ro,z",
        "-e", f"OMP_NUM_THREADS={n_omp}",
        "-e", f"GOMP_CPU_AFFINITY={gomp_affinity}",
    ]
    if stdin_path:
        podman_cmd.append("-i")
    podman_cmd += [
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

    stdin_fh = open(stdin_path, "rb") if stdin_path else None
    try:
        proc = subprocess.Popen(podman_cmd, stdin=stdin_fh, stdout=log_file, stderr=log_file)
        wrapped = _LocalPodmanProc(proc, container_name)
        if proc_holder is not None:
            proc_holder.append(wrapped)
        return wrapped.wait()
    finally:
        if stdin_fh:
            stdin_fh.close()
