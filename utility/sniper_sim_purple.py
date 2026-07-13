"""
sniper_sim_purple.py
Purple (CentOS7, yuri@172.20.2.220) 上の Jin 本人のビルド済み Sniper
(/home/jin/sniper-detloc-backup) を SSH 経由で叩く実行バックエンド。

podman を使う sniper_sim_sid.run_sniper と同じシグネチャを持ち、run.py /
ultra_orchestrator から差し替え可能(podman不可のPurpleを実行資源として
使うための代替パス)。

前提:
  - Purple上に ~/claudex_akarin/{binary,cfg,out} が用意済み
  - binary/ 配下はClaudeXSniperのbinary/とパス構造をミラー(rsyncで同期済み)。
    2026-07-14、binary/直下をGCC15版がデフォルトを占める構成に再編した
    (NPB3.4-OMP/bin、GAPBS、Rodinia/openmp/lavaMD、PARSEC/*、
    PARSEC/splash2/ext/splash2/apps/*等。旧GCC7.3.1版はbinary/GCC7/へ
    退避)。Purple側のミラーは本更新時点でまだ再同期していないため、
    Purple経由でジョブを投げる前にrsyncでの再同期が必要。
  - GRAPHITE_ROOT=/home/jin/sniper-detloc-backup, PIN_ROOT=agungmのPin 3.7

SSH切断への耐性:
  hiragahama-Purple間のSSHはいつ切れても(VPN切断・ネットワーク瞬断等)
  オーケストレータ全体が巻き込まれて止まらないように設計する。
  - ServerAlive設定により、接続が実際には死んでいるのに応答待ちで
    無限にハングすることを防ぐ(生死判定を最大45秒程度で確定させる)。
  - scp/rsync/ssh の各段階は個別に返り値を見て非ゼロなら即座に失敗として
    返すだけで、例外送出も他ジョブへの影響もない
    (呼び出し元 ultra_orchestrator.run_job は1ジョブ単位で completely
    independent に失敗処理・pool.release するため、SSH切断は
    「そのジョブが失敗扱いになる」以上の副作用を持たない)。
"""

import hashlib
import os
import subprocess
import threading

from config.generate_config import TOTAL_SIM_CORES
from utility.width_profile import probe_and_record_remote

SSH_HOST    = "yuri@172.20.2.220"
REMOTE_HOME = "/home/gp.sc.cc.tohoku.ac.jp/yuri"
REMOTE_ROOT = f"{REMOTE_HOME}/claudex_akarin"
REMOTE_BINARY_ROOT = f"{REMOTE_ROOT}/binary"
REMOTE_CFG_ROOT     = f"{REMOTE_ROOT}/cfg"
REMOTE_OUT_ROOT     = f"{REMOTE_ROOT}/out"


# JinのSniperバックアップ・agungmのPinキットは他人の共有ディレクトリのため直接参照/変更
# しない(2026-07-05方針)。必要な範囲だけyuri自身の~/claudex_akarin/配下にコピーし、
# GAPBS修正パッチ適用・O0リビルドもすべてこのコピー内で行っている。
SNIPER_ROOT = f"{REMOTE_ROOT}/sniper-detloc"
PIN_ROOT    = f"{REMOTE_ROOT}/pin_kit_o0"

LOCAL_BINARY_BASE = "/home/hiragahama/ClaudeXSniper/binary"

# 接続が死んでいる場合に無限待ちしないための設定。
# ServerAliveInterval=15 x CountMax=3 で最大45秒以内に切断を検知して終了する。
# BatchMode=yes でパスワード入力待ちにもならない(鍵認証前提、失敗時は即エラー)。
SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
]

# 2026-07-12: SSH多重化(ControlMaster)でMaxStartupsバースト拒否(ret=255)を回避する。
# 1ジョブにつきssh/scpを最大6回張っていたのを、少数の既認証接続の使い回しに変える。
# 1本のControlMasterはMaxSessions(既定10)で頭打ちになるため、PURPLE_CAPACITY_DEFAULT=45
# (width単位)を最小ジョブ幅(2TH)で埋め尽くした場合の最大同時ジョブ数(約22)に、
# 起動/回収時の瞬間的な重複分の余裕を持たせて6本(合計60チャンネル)に分散する。
SSH_CONTROL_SHARDS = 6


def ssh_opts_for(shard_key: str) -> list[str]:
    """ジョブ固有のキー(output_dir等)から決定論的にControlMasterのシャードを選び、
    多重化オプション付きのSSHオプション列を返す。同じジョブは常に同じシャードを使う。"""
    shard = int(hashlib.md5(shard_key.encode()).hexdigest(), 16) % SSH_CONTROL_SHARDS
    return [
        *SSH_OPTS,
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath=~/.ssh/cm-purple-{shard}",
        "-o", "ControlPersist=600",
    ]


def _remote_binary_path(local_binary_path: str) -> str:
    """ローカルのbinary/配下パスをPurple側の同じ相対パスへ変換する。"""
    if not local_binary_path.startswith(LOCAL_BINARY_BASE):
        raise ValueError(f"binary_path が {LOCAL_BINARY_BASE} 配下ではない: {local_binary_path}")
    rel = local_binary_path[len(LOCAL_BINARY_BASE):].lstrip("/")
    return f"{REMOTE_BINARY_ROOT}/{rel}"


class _RemoteJobProc:
    """
    proc_holderに積むラッパー。.kill()した際に、ローカルsshクライアントだけでなく
    Purple側のプロセスグループ(run-sniper→python→lib/sniper/pinbin一式)も
    明示的に終了させる。

    背景: リモートコマンドをsetsidの下で実行し、そのPID(=PGID)をリモート側の
    ファイルに書き出しておく。ローカルsshクライアントをkillしてもTCP接続が
    切れるだけで、非対話的なssh実行では子プロセスへのSIGHUP伝播が保証されず、
    実際にPurple上で270個超のプロセスがinitに再親化されて生き残る事故が
    発生した(2026-07-05)。そのためタイムアウト時は必ずリモート側も
    プロセスグループ単位で明示的にkillする。
    """

    def __init__(self, local_proc: subprocess.Popen, remote_pgid_file: str, ssh_opts: list[str]):
        self._local = local_proc
        self._remote_pgid_file = remote_pgid_file
        self._remote_killed = False
        self._ssh_opts = ssh_opts

    def wait(self) -> int:
        return self._local.wait()

    def kill(self) -> None:
        self._local.kill()
        if self._remote_killed:
            return
        self._remote_killed = True
        try:
            subprocess.run(
                ["ssh", *self._ssh_opts, SSH_HOST,
                 f"[ -f {self._remote_pgid_file} ] && "
                 f"kill -9 -$(cat {self._remote_pgid_file}) 2>/dev/null; true"],
                timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # リモートkillが失敗/タイムアウトしても呼び出し元には影響させない


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
    SSH経由でPurple上のJin本人ビルドSniperを実行し、結果をoutput_dirへ回収する。

    Parameters は sniper_sim_sid.run_sniper と同一。bench_class : width_profile
    (utility.width_profile)の収束検知プローブに使う(2026-07-12、SID側に続き実装)。
    podmanコンテナが無いため、.remote_pgidファイルに記録済みのプロセスグループを
    SSH経由でpsして代用する(probe_and_record_remote/_sample_cpu_pct_remote参照)。
    binary_path/config_path は
    ホスト(hiragahama)上の絶対パスを渡すこと(config_pathは実行前にPurpleへ転送、
    binary_pathはミラー済みPurple側パスへ変換して参照する)。
    stdin_path : 標準入力から読むワークロード(water_nsquared等)用の入力ファイル
                (ホスト上の絶対パス)。指定時はPurpleへ転送しリダイレクトする。
    """
    os.makedirs(output_dir, exist_ok=True)

    # ジョブごとに一意なoutput_dirをキーにControlMasterのシャードを固定する
    # (このジョブに関する全ssh/scp/rsync呼び出しが同じ多重化接続を使い回す)。
    opts = ssh_opts_for(output_dir)
    opts_str = " ".join(opts)

    n_omp = omp_num_threads if omp_num_threads is not None else num_threads
    gomp_affinity = " ".join(str(cpu_map[i]) for i in range(n_omp))

    cfg_name = os.path.basename(config_path)
    remote_cfg_path = f"{REMOTE_CFG_ROOT}/{cfg_name}"
    remote_binary   = _remote_binary_path(binary_path)
    remote_binary_dir = os.path.dirname(remote_binary)

    # cfgをPurpleへ転送(ジョブごとに一意なファイル名なので並列実行でも衝突しない)
    try:
        scp_ret = subprocess.run(
            ["scp", "-q", *opts, config_path, f"{SSH_HOST}:{remote_cfg_path}"],
            stdout=log_file, stderr=log_file,
        )
    except OSError as e:
        log_file.write(f"[sniper_sim_purple] cfg転送でSSH起動失敗: {e}\n")
        return 255
    if scp_ret.returncode != 0:
        log_file.write(f"[sniper_sim_purple] cfg転送失敗(SSH切断の可能性): {config_path}\n")
        return scp_ret.returncode

    # map_file(scheduler/pinned_map用、config_pathと同じディレクトリにconfig_path.
    # generate_config()が生成、2026-07-10)もPurpleへ転送する。cfgの
    # map_file=行が指すREMOTE_CFG_ROOT配下のパスと一致させる必要がある。
    map_path = os.path.splitext(config_path)[0] + ".map"
    if os.path.exists(map_path):
        remote_map_path = f"{REMOTE_CFG_ROOT}/{os.path.basename(map_path)}"
        try:
            scp_ret = subprocess.run(
                ["scp", "-q", *opts, map_path, f"{SSH_HOST}:{remote_map_path}"],
                stdout=log_file, stderr=log_file,
            )
        except OSError as e:
            log_file.write(f"[sniper_sim_purple] map_file転送でSSH起動失敗: {e}\n")
            return 255
        if scp_ret.returncode != 0:
            log_file.write(f"[sniper_sim_purple] map_file転送失敗(SSH切断の可能性): {map_path}\n")
            return scp_ret.returncode

    remote_stdin_path = None
    if stdin_path:
        remote_stdin_path = f"{REMOTE_CFG_ROOT}/{os.path.basename(stdin_path)}"
        try:
            scp_ret = subprocess.run(
                ["scp", "-q", *opts, stdin_path, f"{SSH_HOST}:{remote_stdin_path}"],
                stdout=log_file, stderr=log_file,
            )
        except OSError as e:
            log_file.write(f"[sniper_sim_purple] stdin転送でSSH起動失敗: {e}\n")
            return 255
        if scp_ret.returncode != 0:
            log_file.write(f"[sniper_sim_purple] stdin転送失敗(SSH切断の可能性): {stdin_path}\n")
            return scp_ret.returncode

    run_id = os.path.basename(output_dir.rstrip("/"))
    remote_out_dir = f"{REMOTE_OUT_ROOT}/{run_id}"
    remote_pgid_file = f"{remote_out_dir}/.remote_pgid"

    binary_args_part = f" {binary_args}" if binary_args else ""
    stdin_redirect = f" < {remote_stdin_path}" if remote_stdin_path else ""
    # setsidで新しいプロセスグループを作り、そのPID(=PGID)をファイルに記録する。
    # タイムアウト時にこのPGIDへ一括killできるようにするため(_RemoteJobProc参照)。
    # cdはバイナリ自身のディレクトリに移動する(water_nsquared等がカレント
    # ディレクトリからの相対パスで補助ファイル(random.in等)を読むため)。
    # run-sniperは絶対パスで呼ぶのでこのcd変更でも壊れない(__file__ベースの
    # 内部ツール参照はCWDに依存しないため)。
    inner_cmd = (
        f"cd {remote_binary_dir} && "
        f"export GRAPHITE_ROOT={SNIPER_ROOT} && "
        f"export PIN_ROOT={PIN_ROOT} && "
        f"export OMP_NUM_THREADS={n_omp} && "
        f'export GOMP_CPU_AFFINITY="{gomp_affinity}" && '
        # -n はコマンドラインから--general/total_coresを上書きするため、.cfgの
        # TOTAL_SIM_CORES(16固定、Jin方式)と必ず一致させる(sniper_sim_sid.py参照)。
        f"exec {SNIPER_ROOT}/run-sniper -n {TOTAL_SIM_CORES} -d {remote_out_dir} -c {remote_cfg_path} "
        f"-- {remote_binary}{binary_args_part}{stdin_redirect}"
    )
    remote_cmd = (
        f"mkdir -p {remote_out_dir} && "
        f"setsid bash -c 'echo $$ > {remote_pgid_file}; {inner_cmd}'"
    )

    try:
        proc = subprocess.Popen(
            ["ssh", *opts, SSH_HOST, remote_cmd],
            stdout=log_file, stderr=log_file,
        )
    except OSError as e:
        log_file.write(f"[sniper_sim_purple] SSH起動失敗: {e}\n")
        return 255
    if proc_holder is not None:
        proc_holder.append(_RemoteJobProc(proc, remote_pgid_file, opts))

    # 2026-07-12: width_profileの収束検知プローブ(SID側と対になる実装)。
    # podmanコンテナが無いため、.remote_pgidファイルに記録されたプロセスグループ
    # 内のsniper本体プロセスをSSH経由でpsして代用する(_sample_cpu_pct_remote参照)。
    if bench_class:
        probe_thread = threading.Thread(
            target=probe_and_record_remote,
            args=(remote_pgid_file, SSH_HOST, opts,
                  workload, bench_class, num_threads, "purple"),
            daemon=True,
        )
        probe_thread.start()

    ret = proc.wait()

    if ret != 0:
        log_file.write(f"[sniper_sim_purple] リモート実行失敗/SSH切断 ret={ret}\n")
        return ret

    # 結果をhiragahama側のoutput_dirへ回収 (SSH接続が実行後に切れた場合はここで失敗として検出)
    try:
        rsync_ret = subprocess.run(
            ["rsync", "-az", "-e", f"ssh {opts_str}",
             f"{SSH_HOST}:{remote_out_dir}/", f"{output_dir}/"],
            stdout=log_file, stderr=log_file,
        )
    except OSError as e:
        log_file.write(f"[sniper_sim_purple] 結果回収(rsync)でSSH起動失敗: {e}\n")
        return 255
    if rsync_ret.returncode != 0:
        log_file.write(f"[sniper_sim_purple] 結果回収(rsync)失敗(SSH切断の可能性): {remote_out_dir}\n")
        return rsync_ret.returncode

    return 0
