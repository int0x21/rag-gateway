from __future__ import annotations

import argparse
import asyncio
import json

from .scheduled_crawl import run_scheduled_crawl


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="/etc/rag-gateway/config.yaml")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    res = asyncio.run(run_scheduled_crawl(args.config, force=args.force, dry_run_override=(True if args.dry_run else None)))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

