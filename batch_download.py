#!/usr/bin/env python3
"""Dyvine 批量用户作品下载脚本.

用法:
    python batch_download.py users.txt [--api-url http://localhost:8000] \
        [--api-key your-key] [--include-likes] [--max-concurrent 3]

说明:
    从文本文件读取用户 ID（每行一个），向 Dyvine API 发起异步下载任务，
    并轮询所有任务直到完成。支持并发提交和统一的进度报告。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@dataclass
class DownloadJob:
    user_id: str
    operation_id: str | None = None
    status: str = "pending"
    message: str = ""
    progress: float = 0.0
    total_posts: int = 0
    total_downloaded: int = 0
    failed_count: int = 0
    error_details: str | None = None
    submitted_at: float = field(default_factory=time.time)
    completed_at: float | None = None


class DyvineBatchDownloader:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        include_likes: bool = False,
        max_concurrent: int = 3,
        poll_interval: float = 5.0,
        timeout: float = 30.0,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.include_likes = include_likes
        self.max_concurrent = max_concurrent
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    async def _submit_download(
        self, session: aiohttp.ClientSession, user_id: str
    ) -> DownloadJob:
        if self.include_likes:
            url = (
                f"{self.api_url}/api/v1/users/{user_id}/content:download"
                f"?include_posts=true&include_likes=true"
            )
        else:
            url = f"{self.api_url}/api/v1/posts/users/{user_id}/posts:download"

        try:
            async with session.post(
                url, headers=self.headers, timeout=self.timeout
            ) as resp:
                data = await resp.json()
                if resp.status == 202:
                    job = DownloadJob(
                        user_id=user_id,
                        operation_id=data.get("operation_id"),
                        status="submitted",
                        message=data.get("message", "Download scheduled"),
                    )
                    print(
                        f"  [已提交] 用户 {user_id} -> operation_id: {job.operation_id}"
                    )
                    return job
                else:
                    return DownloadJob(
                        user_id=user_id,
                        status="failed",
                        message=data.get("message", f"HTTP {resp.status}"),
                        error_details=str(data),
                    )
        except TimeoutError:
            return DownloadJob(
                user_id=user_id,
                status="failed",
                message="提交超时",
                error_details="Request timeout",
            )
        except Exception as e:
            return DownloadJob(
                user_id=user_id,
                status="failed",
                message=f"提交异常: {e}",
                error_details=str(e),
            )

    async def _poll_status(
        self, session: aiohttp.ClientSession, job: DownloadJob
    ) -> DownloadJob:
        if self.include_likes:
            url = f"{self.api_url}/api/v1/users/operations/{job.operation_id}"
        else:
            url = f"{self.api_url}/api/v1/posts/operations/{job.operation_id}"

        try:
            async with session.get(
                url, headers=self.headers, timeout=self.timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    job.status = data.get("status", "unknown")
                    job.message = data.get("message", "")
                    job.progress = data.get("progress", 0.0)
                    job.total_posts = data.get("total_posts", 0)
                    job.total_downloaded = data.get("total_downloaded", 0)
                    job.failed_count = data.get("failed_count", 0)
                    job.error_details = data.get("error_details")

                    if job.status in ("completed", "failed", "partial"):
                        job.completed_at = time.time()
                elif resp.status == 404:
                    job.status = "not_found"
                    job.message = "任务未找到"
                    job.completed_at = time.time()
                else:
                    job.message = f"轮询 HTTP {resp.status}"
        except TimeoutError:
            job.message = "轮询超时"
        except Exception as e:
            job.message = f"轮询异常: {e}"

        return job

    async def run(self, user_ids: list[str]) -> list[DownloadJob]:
        """执行完整的批量下载流程."""
        print(f"\n{'='*60}")
        print("Dyvine 批量下载启动")
        print(f"  用户总数: {len(user_ids)}")
        print(f"  并发数:   {self.max_concurrent}")
        print(f"  API 地址: {self.api_url}")
        print(f"  下载 likes: {self.include_likes}")
        print(f"{'='*60}\n")

        jobs: list[DownloadJob] = []
        connector = aiohttp.TCPConnector(limit=self.max_concurrent * 2)
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            print("[阶段 1/2] 提交下载任务...")
            semaphore = asyncio.Semaphore(self.max_concurrent)

            async def submit_with_limit(user_id: str) -> DownloadJob:
                async with semaphore:
                    return await self._submit_download(session, user_id)

            tasks = [submit_with_limit(uid) for uid in user_ids]
            jobs = await asyncio.gather(*tasks)

            submitted = [j for j in jobs if j.operation_id]
            failed = [j for j in jobs if not j.operation_id]

            print(f"\n  提交完成: {len(submitted)} 成功, {len(failed)} 失败")
            for j in failed:
                print(f"    - 用户 {j.user_id}: {j.message}")

            if not submitted:
                print("\n没有成功提交的任务，退出。")
                return jobs

            print(f"\n[阶段 2/2] 轮询任务进度 (每 {self.poll_interval:.0f}s)...")
            pending = {j.operation_id: j for j in submitted}
            completed: dict[str, DownloadJob] = {}
            poll_round = 0

            while pending:
                poll_round += 1
                await asyncio.sleep(self.poll_interval)

                results = await asyncio.gather(
                    *[self._poll_status(session, job) for job in pending.values()]
                )

                still_pending: dict[str, DownloadJob] = {}
                for job in results:
                    if job.status in ("completed", "failed", "partial", "not_found"):
                        completed[job.operation_id] = job
                        duration = ""
                        if job.completed_at and job.submitted_at:
                            secs = job.completed_at - job.submitted_at
                            duration = f" ({secs:.0f}s)"
                        print(
                            f"  [{job.status.upper()}] 用户 {job.user_id} "
                            f"{job.total_downloaded}/{job.total_posts} "
                            f"失败:{job.failed_count}{duration}"
                        )
                        if job.error_details:
                            print(f"      错误: {job.error_details}")
                    else:
                        still_pending[job.operation_id] = job

                pending = still_pending

                if pending and poll_round % 3 == 0:
                    print(f"\n  --- 进行中 ({len(pending)} 个) ---")
                    for job in pending.values():
                        print(
                            f"    {job.user_id}: {job.status} "
                            f"({job.progress*100:.1f}%) {job.message}"
                        )
                    print("  ----------------------\n")

        self._print_summary(jobs)
        return jobs

    def _print_summary(self, jobs: list[DownloadJob]) -> None:
        total = len(jobs)
        submitted = [j for j in jobs if j.operation_id]
        completed_ok = [j for j in jobs if j.status == "completed"]
        partial = [j for j in jobs if j.status == "partial"]
        failed = [j for j in jobs if j.status in ("failed", "not_found")]
        total_downloaded = sum(j.total_downloaded for j in submitted)
        total_failed = sum(j.failed_count for j in submitted)

        print(f"\n{'='*60}")
        print("批量下载完成报告")
        print(f"{'='*60}")
        print(f"  总用户数:        {total}")
        print(f"  成功提交:        {len(submitted)}")
        print(f"  完全完成:        {len(completed_ok)}")
        print(f"  部分完成:        {len(partial)}")
        print(f"  失败/未找到:     {len(failed)}")
        print(f"  总下载作品数:    {total_downloaded}")
        print(f"  总失败数:        {total_failed}")
        print(f"{'='*60}\n")

        if failed:
            print("失败详情:")
            for j in failed:
                print(f"  - {j.user_id}: {j.message}")
            print()


def read_user_ids(filepath: str) -> list[str]:
    """从文件中读取用户 ID 列表."""
    path = Path(filepath)
    if not path.exists():
        print(f"错误: 文件不存在 {filepath}", file=sys.stderr)
        sys.exit(1)

    user_ids = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            uid = line.strip()
            if uid and not uid.startswith("#"):
                user_ids.append(uid)

    if not user_ids:
        print(f"错误: 文件 {filepath} 中没有有效的用户 ID", file=sys.stderr)
        sys.exit(1)

    return user_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dyvine 批量用户作品下载脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法：从文件读取用户ID并下载作品
  python batch_download.py users.txt --api-key your-secret-key

  # 同时下载用户的 likes
  python batch_download.py users.txt --api-key key --include-likes

  # 使用自定义 API 地址和更高并发
  python batch_download.py users.txt \
      --api-url http://dyvine.local:8000 \
      --api-key key \
      --max-concurrent 5

  # 使用环境变量传入 API Key（更安全）
  export DYVINE_API_KEY=your-key
  python batch_download.py users.txt

用户 ID 文件格式:
  每行一个用户 ID，支持空行和以 # 开头的注释行。
  示例:
    MS4wLjABAAAA...
    MS4wLjABAAAA...
        """,
    )
    parser.add_argument(
        "input_file",
        help="包含用户 ID 的文本文件路径 (每行一个)",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Dyvine API 地址 (默认: http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API Key (也可通过 DYVINE_API_KEY 环境变量设置)",
    )
    parser.add_argument(
        "--include-likes",
        action="store_true",
        help="同时下载用户喜欢的作品 (通过 /users/{id}/content:download 端点)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="最大并发提交数 (默认: 3)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="轮询间隔秒数 (默认: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="单次请求超时秒数 (默认: 30)",
    )

    args = parser.parse_args()

    dotenv = _load_env_file(Path(".env"))

    api_key = (
        args.api_key
        or os.environ.get("DYVINE_API_KEY")
        or dotenv.get("SECURITY_API_KEY", "")
    )

    api_url = args.api_url
    if not api_url or api_url == "http://localhost:8000":
        env_url = os.environ.get("DYVINE_API_URL")
        if env_url:
            api_url = env_url
        elif dotenv.get("API_HOST"):
            host = dotenv.get("API_HOST", "0.0.0.0")
            port = dotenv.get("API_PORT", "8000")
            api_url = f"http://{host}:{port}"

    if not api_key:
        print(
            "错误: 必须提供 --api-key、DYVINE_API_KEY 环境变量，"
            "或在 .env 中设置 SECURITY_API_KEY",
            file=sys.stderr,
        )
        sys.exit(1)

    user_ids = read_user_ids(args.input_file)

    downloader = DyvineBatchDownloader(
        api_url=args.api_url,
        api_key=api_key,
        include_likes=args.include_likes,
        max_concurrent=args.max_concurrent,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )

    asyncio.run(downloader.run(user_ids))


if __name__ == "__main__":
    main()
