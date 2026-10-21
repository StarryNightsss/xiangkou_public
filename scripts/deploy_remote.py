"""本地构建 → 上传 → 服务器加载 → 重启（国内无法直连 Docker Hub 时的发布方式）。

用法：
  set XK_SSH_PASS=服务器密码
  python scripts/deploy_remote.py            # 全部重建
  python scripts/deploy_remote.py --app      # 只更新 app
  python scripts/deploy_remote.py --go       # 只更新 go-api
  python scripts/deploy_remote.py --verify   # 部署后顺带跑验收
"""
from __future__ import annotations

import argparse
import gzip
import os
import subprocess
import sys
import tempfile
import time

import paramiko

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("XK_SSH_HOST", "119.23.44.74")
USER = os.environ.get("XK_SSH_USER", "root")
PASS = os.environ.get("XK_SSH_PASS", "")
REMOTE_DIR = "/opt/xiangkou"

APP_IMAGE = "xiangkou-app:local"
GO_IMAGE = "xiangkou-go:local"

APP_RUN = (
    "docker run -d --name xk-app --restart unless-stopped --network host "
    "--env-file /opt/xiangkou/app.env "
    "-e PORT=8090 -e DIANPING_BASE_URL=http://127.0.0.1:8081 "
    f"-e DIANPING_WEB_URL=http://{HOST}:8090 "
    "-e XIANGKOU_DATA_PATH=/data/xiangkou.sqlite3 -e XIANGKOU_IMAGES_DIR=/uploads "
    "-e XIANGKOU_CATALOG_ADMIN_IDS=1010 -e XIANGKOU_CATALOG_DEMO_IDS=1012 "
    "-v /opt/xiangkou/data:/data -v /opt/xiangkou/uploads:/uploads "
    f"{APP_IMAGE}"
)

# 数据库连接串从环境变量读取，避免把密码写进仓库。
MYSQL_DSN = os.environ.get("XK_MYSQL_DSN", "")

GO_RUN = (
    "docker run -d --name xk-go --restart unless-stopped --network host "
    "-e PORT=8081 "
    f'-e MYSQL_DSN="{MYSQL_DSN}" '
    "--env-file /opt/xiangkou/smtp.env "
    "-e REDIS_ADDR=127.0.0.1:6379 -e UPLOAD_DIR=/uploads "
    "-e XIANGKOU_PAYMENT_MOCK_ENABLED=true -e XIANGKOU_CATALOG_ADMIN_IDS=1010 -e XIANGKOU_CATALOG_DEMO_IDS=1012 "
    "-e RECONCILE_INTERVAL_SECONDS=30 "
    "-v /opt/xiangkou/uploads:/uploads "
    f"{GO_IMAGE}"
)


def run(cmd: list[str], **kwargs) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True, **kwargs)


def ssh() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASS, timeout=20)
    return client


def remote(client: paramiko.SSHClient, command: str, timeout: int = 600) -> str:
    print("  >", command[:120].replace("\n", " "))
    channel = client.get_transport().open_session()
    channel.settimeout(timeout)
    channel.exec_command(command)
    out = b""
    while True:
        if channel.recv_ready():
            out += channel.recv(4096)
        elif channel.recv_stderr_ready():
            out += channel.recv_stderr(4096)
        elif channel.exit_status_ready():
            break
        else:
            time.sleep(0.1)
    code = channel.recv_exit_status()
    text = out.decode("utf-8", "replace")
    if code != 0:
        print(text[-2000:])
        raise RuntimeError(f"remote command failed ({code})")
    return text


def build(images: list[str]) -> None:
    if APP_IMAGE in images:
        run(["docker", "build", "-f", "deploy/Dockerfile.app", "-t", APP_IMAGE, "."])
    if GO_IMAGE in images:
        run(["docker", "build", "-f", "deploy/Dockerfile.go", "-t", GO_IMAGE, "."])


def package(images: list[str]) -> str:
    fd, tar_path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    gz_path = tar_path + ".gz"
    run(["docker", "save", "-o", tar_path, *images])
    with open(tar_path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=1) as dst:
        while chunk := src.read(8 * 1024 * 1024):
            dst.write(chunk)
    os.remove(tar_path)
    print(f"  package: {os.path.getsize(gz_path)/1_048_576:.1f} MB -> {gz_path}")
    return gz_path


def upload(client: paramiko.SSHClient, local: str) -> None:
    sftp = client.open_sftp()
    size = os.path.getsize(local)
    state = {"last": 0.0}

    def progress(done: int, total: int) -> None:
        if time.time() - state["last"] > 2:
            state["last"] = time.time()
            print(f"\r  upload {done/1_048_576:.1f}/{size/1_048_576:.1f} MB", end="", flush=True)

    sftp.put(local, "/root/images.tgz", callback=progress)
    print()
    sftp.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", action="store_true", help="只更新 app")
    parser.add_argument("--go", action="store_true", help="只更新 go-api")
    parser.add_argument("--verify", action="store_true", help="部署后跑验收")
    args = parser.parse_args()

    if not PASS:
        print("请先设置 XK_SSH_PASS 环境变量")
        return 2

    images = []
    if (args.go or not args.app) and not MYSQL_DSN:
        raise SystemExit("请先设置环境变量 XK_MYSQL_DSN，例如 "
                         "xk:密码@tcp(127.0.0.1:3306)/xiangkou?parseTime=true&charset=utf8mb4&loc=Local")
    if args.app and not args.go:
        images = [APP_IMAGE]
    elif args.go and not args.app:
        images = [GO_IMAGE]
    else:
        images = [APP_IMAGE, GO_IMAGE]

    print("== 1/4 本地构建 ==")
    build(images)

    print("== 2/4 打包镜像 ==")
    archive = package(images)

    print("== 3/4 上传并加载 ==")
    client = ssh()
    try:
        upload(client, archive)
        remote(client, "docker load -i /root/images.tgz")
        print("== 4/4 重启服务 ==")
        # 只重建要更新的容器，避免把另一个服务一起删掉。
        if GO_IMAGE in images:
            remote(client, "docker rm -f xk-go >/dev/null 2>&1 || true")
            remote(client, GO_RUN)
        if APP_IMAGE in images:
            remote(client, "docker rm -f xk-app >/dev/null 2>&1 || true")
            remote(client, APP_RUN)
        time.sleep(6)
        health = remote(client, "curl -sS -m 10 http://127.0.0.1:8090/health")
        print("  health:", health.strip())
        if args.verify:
            print("== 验收 ==")
            print(remote(client, (
                "curl -sS -m 5 -o /dev/null -w 'page:%{http_code}\\n' http://127.0.0.1:8090/ ; "
                "curl -sS -m 5 -o /dev/null -w 'go-api:%{http_code}\\n' http://127.0.0.1:8081/shop-type/list ; "
                "curl -sS -m 5 -o /dev/null -w 'public:%{http_code}\\n' http://" + HOST + ":8090/"
            )))
    finally:
        client.close()
        os.remove(archive)
    print(f"完成：http://{HOST}:8090")
    return 0


if __name__ == "__main__":
    sys.exit(main())
