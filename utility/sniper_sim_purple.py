"""
sniper_sim_purple.py
Purple (CentOS7, yuri@172.20.2.220) 上の Jin 本人のビルド済み Sniper
(/home/jin/sniper-detloc-backup) を SSH 経由で叩く実行バックエンド。

podman を使う sniper_sim_sid.run_sniper と同じシグネチャを持ち、run.py /
ultra_orchestrator から差し替え可能(podman不可のPurpleを実行資源として
使うための代替パス)。

前提:
  - Purple上に ~/claudex_akarin/{binary,cfg,out} が用意済み
  - binary/ 配下は ClaudeXSniper/binary/{NPB3.3-OMP/bin, GAPBS,
    Rodinia/openmp/lavaMD} とパス構造をミラー(rsyncで同期済み)
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

import os
import subprocess

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
_SSH_OPTS_STR = " ".join(SSH_OPTS)


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

    def __init__(self, local_proc: subprocess.Popen, remote_pgid_file: str):
        self._local = local_proc
        self._remote_pgid_file = remote_pgid_file
        self._remote_killed = False

    def wait(self) -> int:
        return self._local.wait()

    def kill(self) -> None:
        self._local.kill()
        if self._remote_killed:
            return
        self._remote_killed = True
        try:
            subprocess.run(
                ["ssh", *SSH_OPTS, SSH_HOST,
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
) -> int:
    """
    SSH経由でPurple上のJin本人ビルドSniperを実行し、結果をoutput_dirへ回収する。

    Parameters は sniper_sim_sid.run_sniper と同一。binary_path/config_path は
    ホスト(hiragahama)上の絶対パスを渡すこと(config_pathは実行前にPurpleへ転送、
    binary_pathはミラー済みPurple側パスへ変換して参照する)。
    stdin_path : 標準入力から読むワークロード(water_nsquared等)用の入力ファイル
                (ホスト上の絶対パス)。指定時はPurpleへ転送しリダイレクトする。
    """
    os.makedirs(output_dir, exist_ok=True)

    n_omp = omp_num_threads if omp_num_threads is not None else num_threads
    gomp_affinity = " ".join(str(cpu_map[i]) for i in range(n_omp))

    cfg_name = os.path.basename(config_path)
    remote_cfg_path = f"{REMOTE_CFG_ROOT}/{cfg_name}"
    remote_binary   = _remote_binary_path(binary_path)
    remote_binary_dir = os.path.dirname(remote_binary)

    # cfgをPurpleへ転送(ジョブごとに一意なファイル名なので並列実行でも衝突しない)
    try:
        scp_ret = subprocess.run(
            ["scp", "-q", *SSH_OPTS, config_path, f"{SSH_HOST}:{remote_cfg_path}"],
            stdout=log_file, stderr=log_file,
        )
    except OSError as e:
        log_file.write(f"[sniper_sim_purple] cfg転送でSSH起動失敗: {e}\n")
        return 255
    if scp_ret.returncode != 0:
        log_file.write(f"[sniper_sim_purple] cfg転送失敗(SSH切断の可能性): {config_path}\n")
        return scp_ret.returncode

    remote_stdin_path = None
    if stdin_path:
        remote_stdin_path = f"{REMOTE_CFG_ROOT}/{os.path.basename(stdin_path)}"
        try:
            scp_ret = subprocess.run(
                ["scp", "-q", *SSH_OPTS, stdin_path, f"{SSH_HOST}:{remote_stdin_path}"],
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
        f"exec {SNIPER_ROOT}/run-sniper -n {num_threads} -d {remote_out_dir} -c {remote_cfg_path} "
        f"-- {remote_binary}{binary_args_part}{stdin_redirect}"
    )
    remote_cmd = (
        f"mkdir -p {remote_out_dir} && "
        f"setsid bash -c 'echo $$ > {remote_pgid_file}; {inner_cmd}'"
    )

    try:
        proc = subprocess.Popen(
            ["ssh", *SSH_OPTS, SSH_HOST, remote_cmd],
            stdout=log_file, stderr=log_file,
        )
    except OSError as e:
        log_file.write(f"[sniper_sim_purple] SSH起動失敗: {e}\n")
        return 255
    if proc_holder is not None:
        proc_holder.append(_RemoteJobProc(proc, remote_pgid_file))
    ret = proc.wait()

    if ret != 0:
        log_file.write(f"[sniper_sim_purple] リモート実行失敗/SSH切断 ret={ret}\n")
        return ret

    # 結果をhiragahama側のoutput_dirへ回収 (SSH接続が実行後に切れた場合はここで失敗として検出)
    try:
        rsync_ret = subprocess.run(
            ["rsync", "-az", "-e", f"ssh {_SSH_OPTS_STR}",
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
