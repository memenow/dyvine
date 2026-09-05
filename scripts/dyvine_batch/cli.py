"""Argument parsing and entry point for the batch download CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import runners
from .config import (
    DEFAULT_API_PREFIX,
    DEFAULT_API_URL,
    SettingsError,
    read_user_ids,
    resolve_settings,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``serial`` / ``concurrent`` subcommand parser."""
    parser = argparse.ArgumentParser(
        prog="dyvine_batch",
        description="Dyvine 批量用户作品下载脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 并发下载（推荐）
  python -m scripts.dyvine_batch concurrent users.txt --api-key KEY

  # 串行下载（一次一个用户，便于观察）
  python -m scripts.dyvine_batch serial users.txt --api-key KEY

  # 同时下载用户的 likes
  python -m scripts.dyvine_batch concurrent users.txt --api-key KEY --include-likes

  # 使用环境变量传入 API Key（更安全）
  export DYVINE_API_KEY=your-key
  python -m scripts.dyvine_batch concurrent users.txt

用户 ID 文件格式:
  每行一个用户 ID，支持空行和以 # 开头的注释行。
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("concurrent", "并发提交所有用户并统一轮询（原 batch_download.py）"),
        ("serial", "逐个用户提交并轮询（原 download_serial.py）"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("input_file", help="包含用户 ID 的文本文件路径 (每行一个)")
        sub.add_argument(
            "--api-url",
            default=None,
            help=("Dyvine API 地址 " f"(默认: DYVINE_API_URL 或 {DEFAULT_API_URL})"),
        )
        sub.add_argument(
            "--api-key",
            default=None,
            help="API Key (也可通过 DYVINE_API_KEY 环境变量设置)",
        )
        sub.add_argument(
            "--api-prefix",
            default=None,
            help=("API 路径前缀 " f"(默认: DYVINE_API_PREFIX 或 {DEFAULT_API_PREFIX})"),
        )
        sub.add_argument(
            "--include-likes",
            action="store_true",
            help="同时下载用户喜欢的作品 (通过 /users/{id}/content:download 端点)",
        )
        sub.add_argument(
            "--max-concurrent",
            type=int,
            default=3,
            help="最大并发提交数 (默认: 3；serial 模式忽略)",
        )
        sub.add_argument(
            "--poll-interval",
            type=float,
            default=5.0,
            help="轮询间隔秒数 (默认: 5)",
        )
        sub.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="单次请求超时秒数 (默认: 30)",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code.

    Exit codes: 0 on success (even when individual downloads fail —
    those are reported, not errors), 1 on configuration/input errors
    (including unreadable input files), 2 on CLI usage errors
    (``argparse`` exits before this function runs) or unexpected
    crashes, 130 on keyboard interrupt.
    """
    args = build_parser().parse_args(argv)
    try:
        settings = resolve_settings(
            api_url=args.api_url,
            api_key=args.api_key,
            api_prefix=args.api_prefix,
            include_likes=args.include_likes,
            max_concurrent=args.max_concurrent,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        user_ids = read_user_ids(args.input_file)
    except (OSError, UnicodeDecodeError) as exc:
        # ``FileNotFoundError`` (an ``OSError``) plus siblings such as
        # ``IsADirectoryError``/``PermissionError``, and undecodable
        # bytes: all input-file problems, all exit 1, never a
        # traceback.
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except SettingsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    runner = (
        runners.run_concurrent if args.command == "concurrent" else runners.run_serial
    )
    try:
        asyncio.run(runner(settings, user_ids))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except Exception as exc:  # defensive: never traceback on operators
        print(f"错误: 运行失败: {exc}", file=sys.stderr)
        return 2
    return 0
