#!/usr/bin/env python3
"""串行下载脚本：逐个用户发起下载请求并轮询完成。"""

import os
import sys
import time
from pathlib import Path

import httpx


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    return env


dotenv = _load_env_file(Path(".env"))

API_URL = os.environ.get("DYVINE_API_URL") or "http://localhost:8000"
if API_URL == "http://localhost:8000" and dotenv.get("API_HOST"):
    host = dotenv.get("API_HOST", "0.0.0.0")
    port = dotenv.get("API_PORT", "8000")
    API_URL = f"http://{host}:{port}"

API_KEY = os.environ.get("DYVINE_API_KEY") or dotenv.get("SECURITY_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}


def submit(user_id: str) -> dict:
    url = f"{API_URL}/api/v1/posts/users/{user_id}/posts:download"
    r = httpx.post(url, headers=HEADERS, timeout=30)
    return r.json()


def poll(operation_id: str) -> dict:
    url = f"{API_URL}/api/v1/posts/operations/{operation_id}"
    r = httpx.get(url, headers=HEADERS, timeout=30)
    return r.json()


def download_user(user_id: str) -> dict:
    print(f"\n{'='*50}")
    print(f"用户: {user_id}")
    print(f"{'='*50}")

    resp = submit(user_id)
    if "operation_id" not in resp:
        print(f"  提交失败: {resp.get('message', resp)}")
        return {"user_id": user_id, "status": "failed", "error": resp}

    oid = resp["operation_id"]
    print(f"  已提交, operation_id: {oid}")

    while True:
        time.sleep(5)
        status = poll(oid)
        s = status.get("status", "unknown")
        downloaded = status.get("total_downloaded", 0)
        total = status.get("total_posts", 0)
        failed = status.get("failed_count", 0)
        progress = status.get("progress", 0) * 100
        msg = status.get("message", "")
        print(
            f"  状态: {s} | 已下载: {downloaded}/{total} | 失败: {failed} | "
            f"进度: {progress:.1f}% | {msg}"
        )
        if s in ("completed", "failed", "partial"):
            break

    return {"user_id": user_id, **status}


def main():
    if not API_KEY:
        print("错误: .env 中未找到 SECURITY_API_KEY", file=sys.stderr)
        sys.exit(1)

    users = [
        line.strip()
        for line in Path("users.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    print(f"共 {len(users)} 个用户，开始串行下载...")
    results = []

    for i, uid in enumerate(users, 1):
        print(f"\n>>> [{i}/{len(users)}]")
        try:
            r = download_user(uid)
            results.append(r)
        except Exception as e:
            print(f"  异常: {e}")
            results.append({"user_id": uid, "status": "error", "error": str(e)})

    print(f"\n{'='*50}")
    print("汇总报告")
    print(f"{'='*50}")
    total_downloaded = 0
    for r in results:
        s = r.get("status", "error")
        d = r.get("total_downloaded", 0)
        total_downloaded += d
        print(f"  {r['user_id'][:20]}...  状态={s}  已下载={d}")
    print(f"\n总下载作品数: {total_downloaded}")


if __name__ == "__main__":
    main()
