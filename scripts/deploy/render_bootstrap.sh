#!/usr/bin/env bash
# 在入口机 A 上渲染 B 的引导脚本：把隧道私钥、A 的 root 公钥、A 的主机指纹填进模板。
# 输出到 /var/www/mjdeploy/<随机 token>/bootstrap_b.sh，经 nginx 临时片段用 HTTPS 下发。
# 用法：scripts/deploy/render_bootstrap.sh   （打印 B 上要执行的一行命令）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
D=/root/mjagent2-deploy
KEY="$D/b_tunnel_key"
[ -f "$KEY" ] || { echo "缺 $KEY：先在 A 上生成隧道密钥（见 docs/deploy-two-servers.md）" >&2; exit 1; }
TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
OUT_DIR="/var/www/mjdeploy/$TOKEN"
install -d -m 755 "$OUT_DIR"
python3 - "$ROOT/scripts/deploy/bootstrap_b.sh.tmpl" "$KEY" "$OUT_DIR/bootstrap_b.sh" <<'PY'
import sys, pathlib
tmpl, key, out = (pathlib.Path(p) for p in sys.argv[1:4])
text = tmpl.read_text(encoding="utf-8")
host_key = pathlib.Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
root_pub = pathlib.Path("/root/.ssh/id_ed25519.pub").read_text().strip()
text = (text
    .replace("__TUNNEL_PRIVATE_KEY__", key.read_text().rstrip("\n"))
    .replace("__A_HOST_KEY_PUB__", f"{host_key[0]} {host_key[1]}")
    .replace("__A_ROOT_PUBKEY__", root_pub))
out.write_text(text, encoding="utf-8")
PY
chmod 644 "$OUT_DIR/bootstrap_b.sh"
echo "已渲染：$OUT_DIR/bootstrap_b.sh"
echo "在 B 上以 root 执行："
echo "  curl -fsSL https://automanju.com/.mjdeploy/$TOKEN/bootstrap_b.sh | bash"
