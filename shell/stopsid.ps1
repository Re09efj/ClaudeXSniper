# emergency_stop_from_local.ps1
#
# TOKENACH(ローカルPC、Windows PowerShell)に置いて使うスクリプト。SID(hiragahama)専用。
# Purpleはこれとは別に対応すること(SID側の shell/emergency_purple_stop.sh を参照)。
#
# 【実行方法についての重要な注意】
# このファイルをダブルクリックしないこと。PowerShellは.ps1実行後(エラー時含む)に
# ウィンドウを即座に閉じる仕様のため、「何も見えずに一瞬で閉じる」ことになる。
# 必ず PowerShell を先に開いてから、そのウィンドウの中で実行すること:
#   cd <このファイルがあるフォルダ>
#   .\emergency_stop_from_local.ps1
# 実行ポリシーで弾かれる場合はどちらかで解除:
#   powershell -ExecutionPolicy Bypass -File .\emergency_stop_from_local.ps1
#   または(恒久設定、推奨): Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
#
# 【中身についての注意・既知の落とし穴】
# 1. パターン文字列の自己マッチ: 単純に `pkill -f "ultra_orchestrator.py"` のような
#    ワンライナーをssh経由で送ると、そのパターン文字列自体が実行中のリモートシェル
#    自身のコマンドラインにも含まれてしまうため、pkillが「自分自身を実行している
#    シェル」まで巻き込んでkillしてしまう(2026-07-11、Purple版で実際に発生し確認済み)。
#    → pgrepで対象PIDを列挙し、自分自身のPID($$)を明示的に除外してからkillする。
# 2. Windows側の引数渡し崩れ: PowerShellから複数行・二重引用符入りの文字列を
#    そのままssh.exeの引数として渡すと、Windows側のコマンドライン解析
#    (PowerShell→ssh.exeへの引数シリアライズ)で引用符が壊れ、リモート側で構文エラーに
#    なることがある(2026-07-12、実機で発生確認。バナー等の単純なechoは通るのに
#    二重引用符を含む行で毎回失敗する、という症状で発覚)。
#    → リモートで実行したいスクリプトをBase64エンコードして1個のトークンとして渡し、
#      リモート側で `base64 -d | bash` にパイプする方式に変更。特殊文字が一切
#      コマンドライン上に出ないため、Windows/Linuxどちらの引用符解釈にも影響されない。
#
# やること(SIDのみ):
#   1. ultra_orchestrator.py をSIGTERM→(3秒待って)SIGKILL
#   2. sniper_* という名前のPodmanコンテナを全部stop
#   3. uptimeを表示して負荷が下がったか確認できるようにする

$maxAttempts = 100
$intervalSec = 2

# シングルクォートのhere-stringなのでPowerShellは中身を一切展開しない
$remoteScript = @'
echo "=== SID Emergency Stop: $(date "+%Y-%m-%d %H:%M:%S") ==="
mypid=$$
echo "--- ultra_orchestrator.py を停止 ---"
found=0
for p in $(pgrep -f "ultra_orchestrator\.py" 2>/dev/null); do
    [ "$p" = "$mypid" ] && continue
    found=1
    kill -TERM "$p" 2>/dev/null
done
if [ "$found" = "1" ]; then
    echo "SIGTERM送信。3秒待機..."
    sleep 3
    for p in $(pgrep -f "ultra_orchestrator\.py" 2>/dev/null); do
        [ "$p" = "$mypid" ] && continue
        kill -KILL "$p" 2>/dev/null
        echo "残っていたPID ${p} にSIGKILL送信"
    done
else
    echo "ultra_orchestrator.py は動いていなかった"
fi
echo "--- sniper_* Podmanコンテナを停止 ---"
CONTAINERS=$(podman ps --format '{{.Names}}' 2>/dev/null | grep '^sniper_')
if [ -n "$CONTAINERS" ]; then
    echo "$CONTAINERS"
    echo "$CONTAINERS" | xargs -r -n1 podman stop -t 5
else
    echo "停止対象のコンテナなし"
fi
echo "--- load average (直後) ---"
uptime
echo "=== 完了 ==="
'@

# Windows側の改行(CRLF)がリモートのbashに渡ると行末に\rが残り、変な挙動の原因に
# なりうるためLFに統一してからエンコードする。
$remoteScript = $remoteScript -replace "`r`n", "`n"
$bytes = [System.Text.Encoding]::UTF8.GetBytes($remoteScript)
$b64   = [Convert]::ToBase64String($bytes)
$remoteCmd = "echo $b64 | base64 -d | bash"

Write-Host "=== Emergency Stop (SID専用): 接続を試行します (最大 $maxAttempts 回 / ${intervalSec}秒間隔) ==="

$success = $false
for ($i = 1; $i -le $maxAttempts; $i++) {
    $timestamp = Get-Date -Format "HH:mm:ss"
    Write-Host "[$i/$maxAttempts] $timestamp 接続試行..."

    ssh -o ConnectTimeout=5 -o BatchMode=yes sid $remoteCmd

    if ($LASTEXITCODE -eq 0) {
        $success = $true
        Write-Host ""
        Write-Host "=== 成功 (試行 $i 回目で接続・実行完了) ==="
        break
    }

    Write-Host "  -> 失敗 (exit=$LASTEXITCODE)。${intervalSec}秒後にリトライ..."
    Start-Sleep -Seconds $intervalSec
}

if (-not $success) {
    Write-Host ""
    Write-Host "=== $maxAttempts 回試行しても接続できませんでした。 ==="
    Write-Host "SIDが完全に応答不能な可能性があります。物理アクセス/コンソール等の別手段を検討してください。"
}

Write-Host ""
Read-Host "Enterキーを押すとウィンドウを閉じます"
