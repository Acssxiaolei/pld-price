#!/usr/bin/env python3
"""每日健康检查：
1. 线上 prices.json 可访问；
2. 最近一次「价格自动同步」workflow 运行成功（说明链路活着）。
异常时向 GITHUB_OUTPUT 写入 msg（供 workflow 创建 Issue 通知）。

注意：价格无变化时 saved_at 停留在上次改价时间是正常现象，
本检查不再用 saved_at 新鲜度判断异常，只看 workflow 是否在正常跑。
"""
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://acssxiaolei.github.io/pld-price/prices.json"
REPO = "Acssxiaolei/pld-price"
WORKFLOW = "sync.yml"
# 允许的最长静默时间：正常每 5 分钟一次，超过 40 分钟没成功 run 才算异常
STALE_MINUTES = 40


def _gh_get(path, token):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "health-check",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        _emit("健康检查缺少 GITHUB_TOKEN，无法查询 workflow 状态")
        return

    # 1. 线上数据可访问
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            data = json.load(resp)
        print(f"线上 prices.json 可访问 | 商品组={len(data.get('groups', []))} | saved_at={data.get('saved_at')}")
    except Exception as e:
        print(f"FAIL 无法获取价格数据: {e}")
        _emit(f"线上价格数据页面无法访问（{e}）")
        return

    # 2. 最近一次 sync.yml run 是否在 STALE_MINUTES 内成功
    try:
        runs = _gh_get(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=5", token)
    except Exception as e:
        print(f"查询 workflow runs 失败: {e}")
        _emit(f"无法查询同步 workflow 状态（{e}）")
        return

    latest = runs["workflow_runs"][0]
    created = datetime.strptime(latest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
    conclusion = latest.get("conclusion")
    status = latest.get("status")
    print(f"最近 run: {latest['created_at']} status={status} conclusion={conclusion} 距今 {age_min:.0f} 分钟")

    if conclusion != "success" or age_min > STALE_MINUTES:
        print("STALE/FAIL")
        _emit(
            f"最近一次「价格自动同步」run 异常："
            f"状态={status} 结论={conclusion} 距今 {age_min:.0f} 分钟"
            f"（阈值 {STALE_MINUTES} 分钟）。请检查腾讯表格分享、cron-job 触发和 Actions 日志。"
        )
    else:
        print("OK")


def _emit(msg):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"msg={msg}\n")


if __name__ == "__main__":
    main()
