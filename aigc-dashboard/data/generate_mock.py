#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIGC 互动大屏运营看板 —— 模拟数据生成器

产出:
  data/mock.json  交付用的模拟数据(标准 JSON)
  data/mock.js    同一份数据挂到 window.MOCK_DATA, 让 index.html 用 file:// 双击即可打开
                  (file:// 下 fetch() 会被浏览器 CORS 策略拦截, 故额外产出一份 js)

随机种子固定, 重复执行结果完全一致。
"""

import json
import math
import random
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path

from hll import (HLL_B, HLL_M, hll_add, hll_decode, hll_encode, hll_estimate,
                 hll_merge, hll_new)

SEED = 42
DAYS = 60                      # 生成 60 天, 看板默认展示近 30 天, 保证"近30天"也有等长的环比窗口
LAST_DAY = date(2026, 9, 15)   # 数据最后一个完整自然日
OPEN_HOUR, CLOSE_HOUR = 10, 22 # 商场营业时间 10:00-22:00
EXPECTED_MINUTES = (CLOSE_HOUR - OPEN_HOUR) * 60  # 每天应在线分钟数 = 720

# 生成时长直方图分桶(秒): 14 个边界 = 13 个桶。
# 存分布而不是存每天的 P95, 是因为分位数不能加权平均 —— 跨切片时先合并
# 直方图再插值。注意插值出的 P95 是**估算值**, 不是精确分位数。
HIST_EDGES = [0, 2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40, 60, 120]

# 用户重复参与的行为参数。participants 是**人次**(触屏会话数);
# 去重人数要靠每条记录里的 HLL 草图跨切片合并才能得到。
REPEAT_SAME_DEVICE = 0.18   # 该次参与是这个点位的回头客
REPEAT_SAME_CITY = 0.03     # 是同城其他点位来过的人
# 回访是聚集在近期的: 喜欢这个玩法的人几天内就会再来, 而不是均匀散在两个月里。
# 所以回头客只从"最近见过的这么多人次"里抽 —— 对一个日均 500 人的点位,
# 5000 大约是最近 10 天。不做这个近期加权的话, 30 天窗口内的复玩几乎观察不到。
RECENT_POOL = 5000

DEVICES = [
    # id,      名称,                  城市,     基准参与人数, 机型
    ("SH-001", "上海·南京东路旗舰店",  "上海", 420, "Gen3-55寸"),
    ("SH-002", "上海·环球港",          "上海", 300, "Gen3-55寸"),
    ("BJ-001", "北京·三里屯太古里",    "北京", 390, "Gen3-65寸"),
    ("BJ-002", "北京·朝阳大悦城",      "北京", 260, "Gen2-55寸"),
    ("SZ-001", "深圳·万象天地",        "深圳", 340, "Gen3-55寸"),
    ("SZ-002", "深圳·海岸城",          "深圳", 230, "Gen2-55寸"),
    ("CD-001", "成都·太古里",          "成都", 310, "Gen3-65寸"),
    ("CD-002", "成都·IFS国金中心",     "成都", 200, "Gen2-55寸"),
]

# ---------------------------------------------------------------- 故事线设定
# 看板要能看出东西, 所以刻意埋了 5 条可被发现的运营故事:
#  1. SH-002 在 9/5-9/6 整机离线两天(网络机柜断电), 在线率和参与人数断崖
#  2. BJ-001 每逢周末排队严重, 平均生成时长显著拉长
#  3. SZ-002 审核拒绝率长期偏高(点位人群/玩法导致的违规素材多)
#  4. CD-001 从 9/11 起模型报错率上升, 生成成功率跌破 90% 并持续
#  5. 9/12-9/13 品牌做了一次线下活动, 全国参与人数冲高
OFFLINE_DEVICE, OFFLINE_DAYS = "SH-002", {date(2026, 9, 5), date(2026, 9, 6)}
SLOW_DEVICE = "BJ-001"
STRICT_DEVICE = "SZ-002"
DEGRADED_DEVICE, DEGRADED_FROM = "CD-001", date(2026, 9, 11)
CAMPAIGN_DAYS = {date(2026, 9, 12), date(2026, 9, 13)}
#  6. CD-002 在最后一天 20:30 突然断网, 至今未恢复 —— 设备面板上仍是"离线"
LIVE_OFFLINE_DEVICE, LIVE_OFFLINE_FROM_HOUR = "CD-002", 20

rng = random.Random(SEED)

# 每个点位见过的用户 ID(用于抽回头客), 以及按城市索引
_seen_by_device = {d[0]: [] for d in DEVICES}
_seen_by_city = {}
_next_uid = [0]


def _new_uid() -> str:
    _next_uid[0] += 1
    return f"u{_next_uid[0]:07d}"


def draw_users(dev_id: str, city: str, n: int):
    """抽出这天这个点位的 n 次参与分别是谁。

    绝大多数是第一次来的新人, 少数是本点位回头客, 极少数是同城其他点位来过的人。
    返回的是**人次**列表, 里面可以有重复 —— 同一个人一天内玩两次是正常的。
    """
    seen_dev = _seen_by_device[dev_id]
    seen_city = _seen_by_city.setdefault(city, [])
    out = []
    for _ in range(n):
        r = rng.random()
        if r < REPEAT_SAME_DEVICE and seen_dev:
            lo = max(0, len(seen_dev) - RECENT_POOL)
            out.append(seen_dev[rng.randrange(lo, len(seen_dev))])
        elif r < REPEAT_SAME_DEVICE + REPEAT_SAME_CITY and seen_city:
            lo = max(0, len(seen_city) - RECENT_POOL)
            uid = seen_city[rng.randrange(lo, len(seen_city))]
            out.append(uid)
            seen_dev.append(uid)
        else:
            uid = _new_uid()
            out.append(uid)
            seen_dev.append(uid)
            seen_city.append(uid)
    return out


def hist_bucket(seconds: float) -> int:
    for i in range(len(HIST_EDGES) - 1):
        if seconds < HIST_EDGES[i + 1]:
            return i
    return len(HIST_EDGES) - 2


def percentile_from_hist(hist, q=0.95):
    """从直方图线性插值估算分位数 —— 保证任意日期/点位切片下 P95 都可聚合还原。"""
    total = sum(hist)
    if total == 0:
        return 0.0
    target = q * total
    cum = 0
    for i, c in enumerate(hist):
        if cum + c >= target and c > 0:
            lo, hi = HIST_EDGES[i], HIST_EDGES[i + 1]
            return round(lo + (hi - lo) * (target - cum) / c, 1)
        cum += c
    return float(HIST_EDGES[-1])


def gen_duration(slow_factor: float, failed: bool) -> float:
    """成功生成的耗时 ~ 对数正态; slow_factor 模拟排队/降级时的整体拉长。"""
    base = rng.lognormvariate(math.log(6.2), 0.42) * slow_factor
    if failed:                       # 失败样本不计入平均时长, 这里只用于超时判定
        base *= rng.uniform(1.8, 4.0)
    return round(min(base, 119.0), 2)


def build_daily(truth=None):
    """truth 是给精度自测用的 (日期,点位) -> 真实用户集合, 不进交付数据。"""
    if truth is None:
        truth = {}
    records = []
    start = LAST_DAY - timedelta(days=DAYS - 1)
    for offset in range(DAYS):
        day = start + timedelta(days=offset)
        is_weekend = day.weekday() >= 5
        for dev_id, _name, city, base, model in DEVICES:
            # ---------- 参与人数 ----------
            factor = 1.0
            factor *= 1.55 if is_weekend else 1.0
            factor *= 1.0 + 0.10 * math.sin(offset / 7.0)      # 缓慢的周期性波动
            factor *= rng.uniform(0.86, 1.14)                   # 日常噪声
            if day in CAMPAIGN_DAYS:
                factor *= 1.9                                   # 线下活动冲高
            if model.startswith("Gen2"):
                factor *= 0.92                                  # 老机型吸引力略低

            offline = dev_id == OFFLINE_DEVICE and day in OFFLINE_DAYS
            online_minutes = 0 if offline else min(
                EXPECTED_MINUTES,
                int(EXPECTED_MINUTES - max(0, rng.gauss(6, 12)))
            )
            if offline:
                factor = 0.0

            # 最后一天晚间断网: 当天只跑到 20:30
            live_off = dev_id == LIVE_OFFLINE_DEVICE and day == LAST_DAY
            if live_off:
                online_minutes = min(online_minutes, (LIVE_OFFLINE_FROM_HOUR - OPEN_HOUR) * 60 + 30)
                factor *= 0.72

            participants = int(base * factor)

            # ---------- 生成次数与成功率 ----------
            gen_users = int(participants * rng.uniform(0.70, 0.82))
            attempts_per_user = rng.uniform(1.25, 1.6)
            gen_requests = int(gen_users * attempts_per_user)

            fail_rate = rng.uniform(0.025, 0.055)
            if dev_id == DEGRADED_DEVICE and day >= DEGRADED_FROM:
                fail_rate = rng.uniform(0.11, 0.17)             # 模型报错率飙升
            if day in CAMPAIGN_DAYS:
                fail_rate *= 1.45                               # 活动高并发, 失败率被推高
            gen_failed = int(gen_requests * fail_rate)
            gen_success = gen_requests - gen_failed

            # ---------- 生成时长 ----------
            slow = 1.0
            if dev_id == SLOW_DEVICE and is_weekend:
                slow = rng.uniform(1.8, 2.3)                    # 周末排队
            if dev_id == DEGRADED_DEVICE and day >= DEGRADED_FROM:
                slow = rng.uniform(1.35, 1.6)
            if day in CAMPAIGN_DAYS:
                slow *= 1.25

            hist = [0] * (len(HIST_EDGES) - 1)
            total_seconds = 0.0
            for _ in range(gen_success):
                d = gen_duration(slow, failed=False)
                total_seconds += d
                hist[hist_bucket(d)] += 1

            # ---------- 转化(人数口径, 逐层收敛) ----------
            gen_success_users = int(gen_users * (1 - fail_rate) * rng.uniform(0.94, 0.99))
            scan_rate = rng.uniform(0.44, 0.58)
            if slow > 1.5:
                scan_rate *= 0.82                               # 等太久的人直接走了
            qr_scans = int(gen_success_users * scan_rate)
            shares = int(qr_scans * rng.uniform(0.52, 0.68))

            # ---------- 内容审核 ----------
            review_total = gen_success
            reject_rate = rng.uniform(0.03, 0.07)
            if dev_id == STRICT_DEVICE:
                reject_rate = rng.uniform(0.18, 0.26)           # 该点位违规素材长期偏多
            if day in CAMPAIGN_DAYS:
                reject_rate *= 1.2
            review_reject = int(review_total * reject_rate)
            pending_rate = rng.uniform(0.005, 0.02)
            if day >= LAST_DAY - timedelta(days=1):
                pending_rate = rng.uniform(0.03, 0.07)          # 最近两天还有在途待审
            review_pending = int(review_total * pending_rate)
            review_pass = review_total - review_reject - review_pending

            # ---------- 分小时参与人数 ----------
            hourly = [0] * 24
            if participants:
                weights = []
                for h in range(24):
                    if h < OPEN_HOUR or h >= CLOSE_HOUR:
                        weights.append(0.0)
                    else:
                        # 双峰: 午市 13 点 / 晚市 19-20 点
                        w = (math.exp(-((h - 13.5) ** 2) / 4.5) * 0.85
                             + math.exp(-((h - 19.5) ** 2) / 4.0) * 1.0)
                        weights.append(w * rng.uniform(0.85, 1.15))
                s = sum(weights)
                left = participants
                if live_off:
                    for h in range(LIVE_OFFLINE_FROM_HOUR + 1, 24):
                        weights[h] = 0.0
                    weights[LIVE_OFFLINE_FROM_HOUR] *= 0.5
                    s2 = sum(weights)
                    weights = [w for w in weights]
                for h in range(24):
                    if weights[h] == 0:
                        continue
                    v = int(participants * weights[h] / s)
                    hourly[h] = v
                    left -= v
                peak = LIVE_OFFLINE_FROM_HOUR if live_off else 19
                hourly[peak] += left  # 余数并入晚高峰, 保证 sum(hourly) == participants

            # 这天这个点位的每一次参与分别是谁 —— 人次列表, 可重复
            visitors = draw_users(dev_id, city, participants)
            unique_visitors = set(visitors)
            truth[(day.isoformat(), dev_id)] = unique_visitors
            regs = hll_new()
            for uid in unique_visitors:
                hll_add(regs, uid)

            records.append({
                "date": day.isoformat(),
                "device_id": dev_id,
                "participants": participants,
                "participants_unique": len(unique_visitors),
                "participants_hll": hll_encode(regs),
                "gen_users": gen_users,
                "gen_requests": gen_requests,
                "gen_success": gen_success,
                "gen_failed": gen_failed,
                "gen_seconds_total": round(total_seconds, 1),
                "gen_seconds_hist": hist,
                "gen_success_users": gen_success_users,
                "qr_scans": qr_scans,
                "shares": shares,
                "review_total": review_total,
                "review_pass": review_pass,
                "review_reject": review_reject,
                "review_pending": review_pending,
                "online_minutes": online_minutes,
                "expected_minutes": EXPECTED_MINUTES,
                "hourly_participants": hourly,
            })
    return records


INCIDENT_LIBRARY = [
    ("device_offline",  "设备离线",       "设备心跳中断, 大屏无响应"),
    ("gen_timeout",     "生成超时",       "单次生成耗时超过 30s 阈值"),
    ("model_error",     "模型报错",       "生成接口返回 5xx, 素材未产出"),
    ("review_reject",   "审核拒绝率异常", "单日审核拒绝率超过 15% 阈值"),
    ("heartbeat_lost",  "心跳丢失",       "连续 3 次心跳未上报"),
    ("asset_download",  "素材下载失败",   "生成结果下发到大屏失败"),
    ("printer_error",   "打印机异常",     "现场打印模块缺纸或卡纸"),
    ("network_jitter",  "网络抖动",       "上行带宽波动, 生成请求重试率升高"),
]
SUGGESTED_ACTION = {
    "device_offline":  "联系点位值守人员现场复位设备并检查取电/网络; 超过 2 小时未恢复升级至运维工单。",
    "gen_timeout":     "检查该点位到推理集群的链路与当前队列深度; 高峰期临时下调生成分辨率或扩容并发。",
    "model_error":     "拉取失败请求的 trace 定位模型版本; 必要时回滚到上一个稳定版本并暂停该点位新玩法。",
    "review_reject":   "抽查被拒素材, 判断是提示词诱导还是审核规则过严; 调整玩法文案或送审规则阈值。",
    "heartbeat_lost":  "确认是网络问题还是设备假死; 远程重启 Agent, 无效则派单现场处理。",
    "asset_download":  "检查 CDN 回源与本地缓存空间; 清理设备缓存后重试下发。",
    "printer_error":   "通知点位补充耗材并清理卡纸; 同步关闭大屏上的打印入口避免用户空等。",
    "network_jitter":  "联系场地网络方排查; 短期开启弱网降级模式(降低素材码率)。",
}


def _emit_breach(add, info, dev, etype, ongoing=False):
    """把一段连续越界写成一条事件: 未恢复的标 open, 已恢复的标 resolved。"""
    days = info["days"]
    span = f"持续 {days} 天" if days > 1 else "当日"
    detail = f"{info['text']} ({span}, {info['start'].isoformat()} 至 {info['last'].isoformat()})"
    add(info["start"], dev, etype, info["sev"], detail,
        "open" if ongoing else "resolved", hour=info["start"].weekday() % 6 + 11,
        end_day=info["last"])


def build_incidents(records):
    """异常事件: 一部分由上面的故事线必然产生, 一部分是日常随机噪声。"""
    events = []
    by_key = {(r["date"], r["device_id"]): r for r in records}
    start = LAST_DAY - timedelta(days=DAYS - 1)

    def add(day, dev, etype, severity, detail, status, hour=None, minute=None, end_day=None):
        """一条异常事件。

        持续型异常必须带 end_date —— 只记开始日的话, 用户把日期筛到事故中段
        就查不到它了(预警在报、事件表却空着)。看板按"区间相交"过滤。
        """
        t = datetime.combine(day, time(
            hour if hour is not None else rng.randint(OPEN_HOUR, CLOSE_HOUR - 1),
            minute if minute is not None else rng.randint(0, 59),
        ))
        ongoing = status == "open"
        events.append({
            "id": f"INC-{len(events) + 1:04d}",
            "time": t.strftime("%Y-%m-%d %H:%M"),
            "date": day.isoformat(),
            "start_date": day.isoformat(),
            # 未恢复的事件一直延续到数据最后一天
            "end_date": (LAST_DAY if ongoing else (end_day or day)).isoformat(),
            "ongoing": ongoing,
            "device_id": dev,
            "type": etype,
            "severity": severity,
            "detail": detail,
            "status": status,
        })

    # 故事 1: 整机离线
    off_start, off_end = min(OFFLINE_DAYS), max(OFFLINE_DAYS)
    add(off_start, OFFLINE_DEVICE, "device_offline", "critical",
        f"机柜断电导致整机离线, 全天 0 参与 (持续 {len(OFFLINE_DAYS)} 天, "
        f"{off_start.isoformat()} 至 {off_end.isoformat()})",
        "resolved", hour=9, minute=41, end_day=off_end)
    add(off_start, OFFLINE_DEVICE, "heartbeat_lost", "serious",
        "连续 3 次心跳未上报, 判定为离线", "resolved", hour=9, minute=44, end_day=off_end)

    # 故事 6: 最后一天晚间断网, 到现在还没恢复
    add(LAST_DAY, LIVE_OFFLINE_DEVICE, "device_offline", "critical",
        "20:32 起心跳中断, 大屏无响应, 当晚剩余营业时段全部损失", "open", hour=20, minute=32)
    add(LAST_DAY, LIVE_OFFLINE_DEVICE, "network_jitter", "serious",
        "断网前 10 分钟上行丢包率持续升高", "open", hour=20, minute=21)

    # 故事 2 / 3 / 4: 指标越界按"连续区间"开单(和真实告警系统一样做抑制),
    # 同一台设备的同一类异常连续多天只产生 1 条事件, 详情里写明持续天数。
    # 逐日的越界情况由看板前端的规则引擎实时扫描, 不在这里重复堆事件。
    breaches = {"model_error": {}, "gen_timeout": {}, "review_reject": {}}
    for r in sorted(records, key=lambda x: (x["device_id"], x["date"])):
        day = date.fromisoformat(r["date"])
        if day < start or r["participants"] == 0:
            continue
        success_rate = r["gen_success"] / r["gen_requests"] if r["gen_requests"] else 1.0
        avg = r["gen_seconds_total"] / r["gen_success"] if r["gen_success"] else 0.0
        reject_rate = r["review_reject"] / r["review_total"] if r["review_total"] else 0.0
        hit = {
            "model_error": (r["gen_requests"] > 0 and success_rate < 0.90,
                            "critical", f"生成成功率 {success_rate:.1%}, 低于 90% 阈值"),
            "gen_timeout": (avg > 12, "serious", f"平均生成时长 {avg:.1f}s, 超过 12s 阈值"),
            "review_reject": (r["review_total"] > 0 and reject_rate > 0.15,
                              "warning", f"审核拒绝率 {reject_rate:.1%}, 超过 15% 阈值"),
        }
        for etype, (over, sev, text) in hit.items():
            streak = breaches[etype]
            prev = streak.get(r["device_id"])
            if over:
                if prev and prev["last"] == day - timedelta(days=1):
                    prev["last"] = day          # 仍在同一次异常里, 只延长不新开单
                    prev["days"] += 1
                    prev["text"] = text
                else:
                    streak[r["device_id"]] = {"start": day, "last": day, "days": 1,
                                              "sev": sev, "text": text, "emitted": None}
            elif prev:
                streak.pop(r["device_id"])
                _emit_breach(add, prev, r["device_id"], etype)
    for etype, streak in breaches.items():
        for dev, info in streak.items():
            _emit_breach(add, info, dev, etype, ongoing=True)

    # 日常随机噪声
    for _ in range(26):
        day = start + timedelta(days=rng.randint(0, DAYS - 1))
        dev = rng.choice(DEVICES)[0]
        etype, _label, detail = rng.choice([
            e for e in INCIDENT_LIBRARY
            if e[0] in ("asset_download", "printer_error", "network_jitter", "heartbeat_lost")
        ])
        sev = rng.choice(["warning", "warning", "serious"])
        rec = by_key.get((day.isoformat(), dev))
        if rec and rec["participants"] == 0:
            continue
        add(day, dev, etype, sev, detail,
            "open" if day >= LAST_DAY - timedelta(days=2) and rng.random() < 0.6 else "resolved")

    events.sort(key=lambda e: e["time"], reverse=True)
    for i, e in enumerate(events, 1):
        e["id"] = f"INC-{i:04d}"
    return events


def build_devices(records):
    """设备当前状态: 由最后一天的实际数据推导, 不另外编造。"""
    last = {r["device_id"]: r for r in records if r["date"] == LAST_DAY.isoformat()}
    out = []
    for dev_id, name, city, _base, model in DEVICES:
        r = last[dev_id]
        success_rate = r["gen_success"] / r["gen_requests"] if r["gen_requests"] else 0.0
        avg = r["gen_seconds_total"] / r["gen_success"] if r["gen_success"] else 0.0
        online_rate = r["online_minutes"] / r["expected_minutes"]
        if dev_id == LIVE_OFFLINE_DEVICE:
            status, gap = "offline", 86          # 21:58 - 86min ≈ 20:32 断的心跳
        elif online_rate < 0.5:
            status, gap = "offline", rng.randint(180, 600)
        elif success_rate < 0.90 or avg > 12:
            status, gap = "degraded", rng.randint(1, 4)
        else:
            status, gap = "online", rng.randint(0, 3)
        heartbeat = datetime.combine(LAST_DAY, time(21, 58)) - timedelta(minutes=gap)
        out.append({
            "id": dev_id,
            "name": name,
            "city": city,
            "model": model,
            "status": status,
            "last_heartbeat": heartbeat.strftime("%Y-%m-%d %H:%M"),
            "firmware": rng.choice(["v2.4.1", "v2.4.1", "v2.3.8", "v2.5.0-beta"]),
        })
    return out


def measure_hll_accuracy(records, truth, trials=200):
    """随机切片上实测 HLL 估算误差。

    文档里引用的是这里测出来的数, 不是 1.04/sqrt(m) 这个理论值 ——
    理论标准误假设哈希理想均匀, 实际误差要靠对账才知道。
    """
    by_key = {(r["date"], r["device_id"]): r for r in records}
    dates = sorted({r["date"] for r in records})
    dev_ids = [d[0] for d in DEVICES]
    check = random.Random(20260916)
    errs = []
    for _ in range(trials):
        i = check.randrange(len(dates))
        j = check.randrange(i, len(dates))
        devs = check.sample(dev_ids, check.randint(1, len(dev_ids)))
        keys = [(d, dv) for d in dates[i:j + 1] for dv in devs]
        real = set()
        regs = hll_new()
        for k in keys:
            r = by_key.get(k)
            if not r:
                continue
            real |= truth[k]
            hll_merge(regs, hll_decode(r["participants_hll"]))
        if len(real) < 50:            # 基数太小时相对误差没有参考意义
            continue
        errs.append(abs(hll_estimate(regs) - len(real)) / len(real))
    errs.sort()
    return {
        "slices_tested": len(errs),
        "median_abs_err_pct": round(statistics.median(errs) * 100, 2),
        "p95_abs_err_pct": round(errs[int(len(errs) * 0.95)] * 100, 2),
        "max_abs_err_pct": round(errs[-1] * 100, 2),
        "registers": HLL_M,
        "theoretical_stderr_pct": round(104 / (HLL_M ** 0.5), 2),
    }


def main():
    truth = {}
    records = build_daily(truth)
    hll_accuracy = measure_hll_accuracy(records, truth)
    incidents = build_incidents(records)
    devices = build_devices(records)

    payload = {
        "meta": {
            "generated_by": "data/generate_mock.py (seed=42)",
            "note": "全部为模拟数据, 与任何真实业务无关",
            "date_range": [records[0]["date"], records[-1]["date"]],
            "default_range_days": 30,
            "open_hour": OPEN_HOUR,
            "close_hour": CLOSE_HOUR,
            "expected_minutes_per_day": EXPECTED_MINUTES,
            "hist_edges": HIST_EDGES,
            "hist_buckets": len(HIST_EDGES) - 1,
            "hll_registers": HLL_M,
            "hll_accuracy": hll_accuracy,
            "thresholds": {
                "success_rate_min": 0.90,
                "avg_gen_seconds_max": 12,
                "reject_rate_max": 0.15,
                "online_rate_min": 0.95,
                "scan_rate_min": 0.30,
            },
            "suggested_action": SUGGESTED_ACTION,
            "incident_type_labels": {t[0]: t[1] for t in INCIDENT_LIBRARY},
        },
        "devices": devices,
        "daily": records,
        "incidents": incidents,
    }

    here = Path(__file__).resolve().parent
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    (here / "mock.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (here / "mock.js").write_text(
        "/* 由 generate_mock.py 自动生成, 请勿手工编辑。"
        "供 index.html 以 file:// 方式直接打开时读取。 */\n"
        "window.MOCK_DATA = " + text + ";\n", encoding="utf-8")

    p95 = percentile_from_hist([sum(c) for c in zip(*(r["gen_seconds_hist"] for r in records))])
    total_gen = sum(r["gen_success"] for r in records)
    print(f"daily records : {len(records)}")
    print(f"devices       : {len(devices)}")
    print(f"incidents     : {len(incidents)}  (open={sum(1 for e in incidents if e['status']=='open')})")
    print(f"date range    : {records[0]['date']} .. {records[-1]['date']}")
    merged = hll_new()
    for r in records:
        hll_merge(merged, hll_decode(r["participants_hll"]))
    sessions = sum(r["participants"] for r in records)
    print(f"success gens  : {total_gen}, overall P95 (估算) = {p95}s")
    print(f"participants  : {sessions:,} 人次 / 去重估算 {hll_estimate(merged):,.0f} 人 "
          f"(真值 {len(set().union(*truth.values())):,})")
    print(f"HLL accuracy  : {hll_accuracy['slices_tested']} 个随机切片, "
          f"中位误差 {hll_accuracy['median_abs_err_pct']}%, "
          f"P95 {hll_accuracy['p95_abs_err_pct']}%, "
          f"最大 {hll_accuracy['max_abs_err_pct']}%")


if __name__ == "__main__":
    main()
