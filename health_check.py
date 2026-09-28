#!/usr/bin/env python3
"""每日健康检查：线上价格数据新鲜度 + 页面可访问性。
异常时向 GITHUB_OUTPUT 写入 msg（供 workflow 创建 Issue 通知）。"""
import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

URL = "https://acssxiaolei.github.io/pld-price/prices.json"
TZ = ZoneInfo("Asia/Shanghai")


def main():
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            code = resp.status
            data = json.load(resp)
    except Exception as e:
        print(f"FAIL 无法获取价格数据: {e}")
        _emit(f"价格数据页面无法访问（{e}）")
        return

    saved = datetime.strptime(data["saved_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    now = datetime.now(TZ)
    mins = (now - saved).total_seconds() / 60
    print(f"HTTP {code} | 距上次更新 {mins:.0f} 分钟 | saved_at={data['saved_at']} | 商品组={len(data.get('groups', []))}")

    if mins > 40:
        print("STALE")
        _emit(f"价格数据已 {mins:.0f} 分钟未更新（最后更新于 {saved.strftime('%Y-%m-%d %H:%M')}），请检查腾讯表格分享、cron-job 触发和工作流状态。")
    else:
        print("OK")


def _emit(msg):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"msg={msg}\n")


if __name__ == "__main__":
    main()
