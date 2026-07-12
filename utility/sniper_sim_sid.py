"""
sniper_sim_sid.py
podman を使って Sniper コンテナ上でシミュレーションを実行するラッパー(SIDホスト用)。

コンテナ: localhost/snipersim/snipersim:detloc-firsttouch-v12-dedupfix
  - 2026-07-12、detloc-firsttouch-v3(下記)をベースに、LU/2TH/Wの_SetLock
    pthread_mutex所有権UB(setlock.h)・cache_block_info.hのtag/cstate順序
    ギャップ・barrier_sync_server.h/trace_manager.h/shmem_perf_model.ccの
    共有フィールド非atomicアクセスを修正(v9)、さらにTraceManager::
    signalDone()の「アプリ内残りスレッド全員がstall中なら強制resume」への
    一般化(v12、dedupのTRACE段階での複数スレッド同時ハングに対応)を積んだ
    イメージ。LU/dedupそれぞれ実機検証済み(Documents/SniperBugFix.md参照)。
    v3以降の下記の説明は変更なし。
  - 2026-07-10、snipersim/snipersim:latestに以下を段階的に移植・再ビルドした
    イメージ(元のlatestタグは無変更、各段階も別タグで保存済み):
    1. detloc: Jinのscheduler_pinned_mapパッチ(common/scheduler/
       scheduler_pinned_map.cc/h, split_string.cc/h)。GOMP_CPU_AFFINITYが
       この環境で機能しない問題への対応で、map_file(thread_id:cpu_id)による
       厳密な静的配置が必要になったため(config/generate_config.py参照)。
    2. detloc-firsttouch-v2: AddressHomeLookupへのFirst-Touch実装(初版)、
       NetworkModelBusのignore_local_traffic判定・DramDirectoryCntlrの
       DRAM_LOCAL/REMOTE判定のノード単位化。「本棚問題」(getHome()が
       アドレスハッシュのみでコアの物理配置を無視する問題)への対応。
    3. detloc-firsttouch-v3: v2のFirst-Touch実装にあった重大なバグ修正
       (AddressHomeLookupはコアごとに別インスタンスが生成されるため、
       first-touchの記録がコアごとに16個バラバラで、グローバルに1つの
       はずの「持ち主」情報が共有されていなかった。static共有マップに変更)。
       加えてcache_cntlr.cc/hに新しい検証用計測ポイント
       (access-home-local/remote)を追加(既存のloads-where-dram-local/
       remoteはタグディレクトリ⇔DRAMコントローラの内部プロトコル区間しか
       見ておらず、First-Touch後は常に一致するため検証に使えなかった)。
       GUPS(全スレッドが共有テーブル全体へランダムアクセスするワークロード)
       で実測検証: Packed=100%ローカル、Scatter=SID53.3%/Purple73.5%
       ローカルという、配置に応じた妥当な差を確認済み。
       詳細はDocuments/2026年7月10日.md参照。
  - /root/sniper/run-sniper がエントリポイント
  - バイナリは --binary でホストパスを渡し、コンテナに /binary としてマウント
  - 出力は /out にマウント
"""

import os
import subprocess
import threading
import uuid

from config.generate_config import TOTAL_SIM_CORES
from utility.width_profile import probe_and_record

CONTAINER_IMAGE = "localhost/snipersim/snipersim:detloc-firsttouch-v12-dedupfix"
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
    bench_class:    str = "",
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
    bench_class   : ベンチマーククラス(S/W/A等)。utility.width_profileへの実測記録の
                   キーに使う。空文字列の場合はプローブを起動しない(後方互換)。

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

    # GOMP_CPU_AFFINITY: 2026-07-10判明、この環境のSniperの実行時syscall経由の
    # 配置には効かない。実際の配置はconfig側のscheduler/pinned_map(map_file)が
    # 決める(config/generate_config.py参照)。cpu_mapと同じ値なので設定しても
    # 矛盾は起きないが、実効性のない冗長設定である点に注意。
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
        # -n はコマンドラインから--general/total_coresを上書きするため、.cfgの
        # TOTAL_SIM_CORES(16固定、Jin方式)と必ず一致させる。num_threadsを渡すと
        # cfgの設定を無効化してしまい、以前の「範囲外バグ」が復活する。
        "-n", str(TOTAL_SIM_CORES),
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

        # 2026-07-12: 収束検知方式のCPU%プローブ(utility.width_profile)を
        # バックグラウンドスレッドで起動する。ジョブ本体の完了を待たずに
        # daemon threadとして走らせ、収束(または収束前の完了/タイムアウト)を
        # 検知したら自律的に終了する。bench_class未指定(空文字列)の呼び出し元
        # には影響しない(後方互換)。
        if bench_class:
            probe_thread = threading.Thread(
                target=probe_and_record,
                args=(container_name, workload, bench_class, num_threads, "sid"),
                daemon=True,
            )
            probe_thread.start()

        return wrapped.wait()
    finally:
        if stdin_fh:
            stdin_fh.close()
