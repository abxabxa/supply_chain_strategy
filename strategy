#!/usr/bin/env python3
"""
五大美股巨头A股供应链 | 增量抱团策略
全动态候选池 + 增量排名 + Bark推送

Tokens 从环境变量读取（GitHub Actions Secrets 注入）：
    - TUSHARE_TOKEN: Tushare Pro API Token
    - BARK_KEY:      Bark 推送设备 Key
    - BARK_SERVER:   Bark 服务器地址（可选，默认 https://api.day.app）

运行:
    python strategy.py config.yaml              # 完整运行
    python strategy.py config.yaml --test-push  # 测试推送
    python strategy.py config.yaml --dry-run    # 试运行

依赖: pip install tushare requests pyyaml pandas numpy
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml
import pandas as pd
import tushare as ts

logger = logging.getLogger("strategy")

DEFAULT_CONFIG = {
    "top_n": 10,
    "weights": {"fund_delta": 0.50, "inst_delta": 0.30, "base_hold": 0.10, "small_cap_bonus": 0.10},
    "crowding": {"safe": 70, "warning": 80, "danger": 90},
    "allocation": {"fund_gt_15": 12, "fund_8_to_15": 10, "fund_5_to_8": 8, "fund_lt_5": 6},
    "market_cap_bonus": {
        "small": {"max_mv": 2.0, "bonus": 10.0},
        "mid": {"max_mv": 5.0, "bonus": 5.0},
        "mid_large": {"max_mv": 10.0, "bonus": 2.0},
        "large": {"max_mv": 999999, "bonus": 0.0},
    },
    "discovery": {"scan_days": 7, "thresholds": {"confirm": 0.5, "watching": 0.3}},
}

GIANT_KWS = {
    "nvidia": ["英伟达", "NVIDIA", "nvidia", "安谋", "GB200", "B200", "H100", "H200", "Blackwell"],
    "tesla": ["特斯拉", "Tesla", "TESLA", "Optimus", "人形机器人", "Cybertruck", "FSD"],
    "apple": ["苹果", "Apple", "APPLE", "Vision Pro", "iPhone", "M系列芯片"],
    "broadcom": ["博通", "Broadcom", "BROADCOM", "Tomahawk", "交换芯片"],
    "google": ["谷歌", "Google", "GOOGLE", "Alphabet", "Gemini", "TPU", "Waymo"],
}

DIRECT_KWS = ["供应商", "一级供应商", "核心供应商", "tier1", "Tier1", "直接供应", "供货",
    "配套", "代工", "ODM", "OEM", "认证通过", "通过认证", "进入供应链", "纳入供应链",
    "独家供应", "主供", "定点", "量产", "批量供货", "合作协议", "战略协议"]

INDIRECT_KWS = ["光模块", "光器件", "光芯片", "CPO", "硅光", "800G", "1.6T",
    "PCB", "覆铜板", "CCL", "高频覆铜板", "高速PCB",
    "刻蚀设备", "薄膜设备", "清洗设备", "半导体设备", "先进封装", "Chiplet",
    "AI芯片", "GPU", "算力", "智算中心", "数据中心",
    "液冷", "浸没式液冷", "散热", "温控",
    "HBM", "DDR5", "内存接口", "存储芯片",
    "汽车电子", "智能驾驶", "激光雷达", "BMS", "热管理", "线控制动", "一体化压铸",
    "人形机器人", "谐波减速器", "行星减速器", "滚珠丝杠", "空心杯电机", "无框力矩电机",
    "高速铜缆", "DAC", "高频铜箔"]

NEGATIVE_KWS = ["未有合作", "没有供应", "否认", "澄清公告", "不实传闻",
    "终止合作", "取消订单", "退出供应链", "被移除"]


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def get_env_token(name: str, required: bool = True) -> str:
    val = os.getenv(name, "").strip()
    if required and not val:
        raise ValueError(f"环境变量 {name} 未设置。请在 GitHub Actions Secrets 中配置。")
    return val


class TushareClient:
    API_INTERVAL = 0.3

    def __init__(self, token: str = ""):
        self.token = token
        if not self.token:
            raise ValueError("TUSHARE_TOKEN not provided.")
        ts.set_token(self.token)
        self.pro = ts.pro_api()
        self._last_call = 0

    def _call(self, func: str, **kw):
        elapsed = time.time() - self._last_call
        if elapsed < self.API_INTERVAL:
            time.sleep(self.API_INTERVAL - elapsed)
        for attempt in range(3):
            try:
                self._last_call = time.time()
                fn = getattr(self.pro, func)
                df = fn(**kw)
                if df is not None and not df.empty:
                    return df
                return pd.DataFrame()
            except Exception as e:
                msg = str(e)
                if "积分" in msg or "permission" in msg.lower():
                    logger.error(f"{func}: permission denied ({msg})")
                    return None
                if "freq" in msg.lower() or "limit" in msg.lower():
                    time.sleep(2 ** attempt)
                    continue
                logger.warning(f"{func} attempt {attempt + 1}/3: {e}")
                if attempt < 2:
                    time.sleep(1)
        return pd.DataFrame()

    def stock_list(self) -> dict[str, str]:
        df = self._call("stock_basic", exchange="", list_status="L", fields="ts_code,name")
        if df is not None and not df.empty:
            return dict(zip(df["ts_code"], df["name"]))
        return {}

    def fund_hold(self, ts_code: str, period: str | None = None):
        kw = {"ts_code": ts_code}
        if period:
            kw["end_date"] = period
        df = self._call("report_fund_hold", **kw)
        if df is not None and not df.empty and "end_date" in df.columns:
            for c in ["fund_hold", "fund_ratio"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.sort_values("end_date", ascending=False)
        return df if df is not None else pd.DataFrame()

    def top10_holders(self, ts_code: str, period: str | None = None):
        kw = {"ts_code": ts_code}
        if period:
            kw["end_date"] = period
        df = self._call("top10_floatholders", **kw)
        if df is not None and not df.empty:
            if "hold_ratio" in df.columns:
                df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")
            if "end_date" in df.columns:
                df = df.sort_values("end_date", ascending=False)
        return df if df is not None else pd.DataFrame()

    def stock_basic(self, codes: list[str]):
        all_df = self._call("stock_basic", exchange="", list_status="L", fields="ts_code,name,industry,float_share")
        if all_df is None or all_df.empty:
            return pd.DataFrame()
        return all_df[all_df["ts_code"].isin(codes)].copy()

    def news(self, date: str):
        return self._call("major_news", start_date=date, end_date=date, fields="title,content,datetime,src")

    def current_period(self) -> str:
        now = datetime.now()
        y, m = now.year, now.month
        if m in [1, 2, 3]:
            return f"{y - 1}0930"
        elif m in [4, 5, 6, 7]:
            return f"{y - 1}1231"
        elif m in [8, 9, 10]:
            return f"{y}0630"
        return f"{y}0930"

    def hold_data(self, ts_code: str, name: str, period: str | None = None) -> dict:
        res = {"ts_code": ts_code, "name": name, "chain": "",
               "fund_hold": 0, "fund_ratio": 0, "inst_ratio": 0,
               "total_ratio": 0, "float_share": 0,
               "fund_ratio_prev": 0, "inst_ratio_prev": 0,
               "report_period": period or "", "data_source": ""}
        basic = self.stock_basic([ts_code])
        if not basic.empty and "float_share" in basic.columns:
            res["float_share"] = float(basic.iloc[0]["float_share"])
        fund_df = self.fund_hold(ts_code, period)
        if not fund_df.empty:
            latest = fund_df.iloc[0]
            res["fund_hold"] = float(latest.get("fund_hold", 0) or 0)
            if res["float_share"] > 0 and res["fund_hold"] > 0:
                res["fund_ratio"] = (res["fund_hold"] / res["float_share"]) * 100
            res["report_period"] = str(latest.get("end_date", ""))
            res["data_source"] = "report_fund_hold"
        holder_df = self.top10_holders(ts_code, period)
        if not holder_df.empty:
            top10 = holder_df["hold_ratio"].sum() if "hold_ratio" in holder_df.columns else 0
            res["inst_ratio"] = min(top10 * 1.15, 95)
            res["data_source"] += "+top10"
        if res["inst_ratio"] <= res["fund_ratio"]:
            res["inst_ratio"] = res["fund_ratio"] * 1.5
        res["total_ratio"] = res["fund_ratio"] + res["inst_ratio"]
        return res


class DiscoveryEngine:
    POOL_FILE = "candidate_pool.json"

    def __init__(self, client: TushareClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.pool_path = Path(self.POOL_FILE)
        self._cache: dict[str, str] | None = None
        self._cache_time: datetime | None = None

    def _all_stocks(self) -> dict[str, str]:
        if self._cache and self._cache_time and (datetime.now() - self._cache_time).days < 1:
            return self._cache
        self._cache = self.client.stock_list()
        self._cache_time = datetime.now()
        logger.info(f"Loaded {len(self._cache)} A-share stocks")
        return self._cache

    @staticmethod
    def score_text(text: str) -> dict:
        text_l = text.lower()
        matched = {}
        for giant, kws in GIANT_KWS.items():
            s = sum(text_l.count(k.lower()) * 0.3 for k in kws)
            if s > 0:
                matched[giant] = min(s, 1.0)
        if not matched:
            return {"net_score": 0, "matched": {}, "is_sc": False}
        direct = sum(0.25 for kw in DIRECT_KWS if kw.lower() in text_l)
        indirect = sum(0.15 for kw in INDIRECT_KWS if kw.lower() in text_l)
        negative = sum(0.4 for kw in NEGATIVE_KWS if kw.lower() in text_l)
        is_sc = direct > 0.2 or indirect > 0.3
        score = min(direct, 1.0) + min(indirect, 0.8) - min(negative, 1.0) + sum(matched.values()) * 0.3
        return {
            "net_score": round(max(-1, min(2, score)), 3),
            "matched": matched, "is_sc": is_sc, "is_direct": direct > 0.2,
            "direct_score": round(min(direct, 1.0), 3),
            "indirect_score": round(min(indirect, 0.8), 3),
        }

    @staticmethod
    def extract_codes(text: str, stocks: dict[str, str]) -> list[str]:
        codes = set()
        for m in re.finditer(r'(\d{6}\.(?:SZ|SH|BJ))', text.upper()):
            codes.add(m.group(1))
        for code, name in stocks.items():
            if len(name) >= 3 and name in text:
                codes.add(code)
        return list(codes)

    def _scan_news(self, days: int = 7) -> list[dict]:
        signals = []
        stocks = self._all_stocks()
        keywords = [k for kws in GIANT_KWS.values() for k in kws[:3]]
        for kw in keywords:
            for d in range(days):
                date = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
                try:
                    df = self.client.news(date)
                    if df is None or df.empty:
                        continue
                    for _, row in df.iterrows():
                        full = f"{row.get('title', '')} {row.get('content', '')}"
                        sc = self.score_text(full)
                        if sc["net_score"] > 0.3 and sc["is_sc"]:
                            for code in self.extract_codes(full, stocks):
                                signals.append({
                                    "ts_code": code, "name": stocks.get(code, ""),
                                    "title": str(row.get("title", ""))[:100],
                                    "source": f"news:{row.get('src', '')}",
                                    "date": str(row.get("datetime", ""))[:10],
                                    **sc,
                                })
                except Exception:
                    break
        return signals

    @staticmethod
    def _deduplicate(signals: list[dict]) -> list[dict]:
        stock_scores: dict[str, dict] = {}
        for sig in signals:
            code = sig["ts_code"]
            if not sig.get("name"):
                continue
            if code not in stock_scores:
                stock_scores[code] = {
                    "ts_code": code, "name": sig["name"], "net_score": 0,
                    "count": 0, "direct": 0, "indirect": 0,
                    "giants": set(), "evidences": [], "dates": set(), "is_direct": False,
                }
            s = stock_scores[code]
            s["net_score"] = max(s["net_score"], sig["net_score"])
            s["count"] += 1
            if sig.get("is_direct"):
                s["direct"] += 1
                s["is_direct"] = True
            else:
                s["indirect"] += 1
            s["giants"].update(sig.get("matched", {}).keys())
            s["evidences"].append(sig.get("title", ""))
            s["dates"].add(sig.get("date", ""))

        results = []
        gmap = {"nvidia": "英伟达", "tesla": "特斯拉", "apple": "苹果", "broadcom": "博通", "google": "谷歌"}
        for code, s in stock_scores.items():
            bonus = min(s["count"] * 0.1, 0.5)
            final = s["net_score"] + bonus
            giants = "+".join(gmap.get(g, g) for g in sorted(s["giants"]))
            sc_type = "直接供应" if s["is_direct"] else "间接供应"
            results.append({
                "ts_code": code, "name": s["name"],
                "chain": f"{giants}-{sc_type}",
                "score": round(final, 3),
                "signal_count": s["count"],
                "is_direct": s["is_direct"],
                "evidence": "; ".join(s["evidences"][:3]),
                "first_seen": min(s["dates"]) if s["dates"] else datetime.now().strftime("%Y-%m-%d"),
                "last_seen": max(s["dates"]) if s["dates"] else datetime.now().strftime("%Y-%m-%d"),
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def run_scan(self) -> list[dict]:
        logger.info("DiscoveryEngine: Full A-Share Scan")
        if not self._all_stocks():
            return self.load_pool()
        all_signals = self._scan_news(days=self.cfg.get("discovery", {}).get("scan_days", 7))
        scored = self._deduplicate(all_signals)
        th = self.cfg.get("discovery", {}).get("thresholds", {})
        confirm_th = th.get("confirm", 0.5)
        candidates = []
        for s in scored:
            if s["score"] >= confirm_th:
                s["status"] = "confirmed"
                candidates.append(s)
        logger.info(f"Signals: {len(all_signals)}, Unique: {len(scored)}, Confirmed: {len(candidates)}")
        results = []
        for c in candidates:
            results.append({
                "code": c["ts_code"], "name": c["name"], "chain": c["chain"],
                "score": c["score"], "signal_count": c["signal_count"],
                "is_direct": c["is_direct"], "evidence": c["evidence"][:200],
                "first_seen": c["first_seen"], "last_seen": c["last_seen"], "status": "confirmed",
            })
        self.save_pool(results)
        return results

    def load_pool(self) -> list[dict]:
        if not self.pool_path.exists():
            return []
        try:
            with open(self.pool_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [s for s in data.get("stocks", []) if s.get("status") == "confirmed"]
        except Exception as e:
            logger.error(f"Load pool failed: {e}")
            return []

    def save_pool(self, stocks: list[dict]):
        data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": len(stocks),
            "stocks": stocks,
        }
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def get_new(current: list[dict], prev_codes: set[str]) -> list[dict]:
        curr = {s["code"] for s in current}
        new = curr - prev_codes
        return [s for s in current if s["code"] in new]


class SupplyChainStrategy:
    def __init__(self, cfg: dict):
        self.w = cfg.get("weights", DEFAULT_CONFIG["weights"])
        self.th = cfg.get("crowding", DEFAULT_CONFIG["crowding"])
        self.alloc = cfg.get("allocation", DEFAULT_CONFIG["allocation"])
        self.mcap = cfg.get("market_cap_bonus", DEFAULT_CONFIG["market_cap_bonus"])

    def calc_score(self, item: dict) -> float:
        fq2 = item.get("fund_ratio", 0)
        fq1 = item.get("fund_ratio_prev", 0)
        iq2 = item.get("inst_ratio", 0)
        iq1 = item.get("inst_ratio_prev", 0)
        float_share = item.get("float_share", 0)
        fd = max(fq2 - fq1, 0)
        ind = max(iq2 - iq1, 0)
        mv = float_share / 10000
        bonus = (
            self.mcap["small"]["bonus"] if mv < self.mcap["small"]["max_mv"] else
            self.mcap["mid"]["bonus"] if mv < self.mcap["mid"]["max_mv"] else
            self.mcap["mid_large"]["bonus"] if mv < self.mcap["mid_large"]["max_mv"] else
            self.mcap["large"]["bonus"]
        )
        score = fd * self.w["fund_delta"] + ind * self.w["inst_delta"] + (fq2 + iq2) * self.w["base_hold"] + bonus * self.w["small_cap_bonus"]
        item["fund_delta"] = round(fd, 2)
        item["inst_delta"] = round(ind, 2)
        item["score"] = round(score, 2)
        return score

    def rank(self, data: list[dict]) -> list[dict]:
        valid = []
        for item in data:
            fd = item.get("fund_ratio", 0) - item.get("fund_ratio_prev", 0)
            if fd > 0:
                self.calc_score(item)
                valid.append(item)
        if not valid:
            for item in data:
                item["fund_delta"] = 0
                item["inst_delta"] = 0
                item["score"] = item.get("fund_ratio", 0) * 0.4 + item.get("inst_ratio", 0) * 0.3
                valid.append(item)
        valid.sort(key=lambda x: x["score"], reverse=True)
        return valid[: self.alloc.get("top_n", 10)]

    def _crowding(self, ratio: float) -> str:
        if ratio >= self.th.get("danger", 90):
            return "extreme"
        if ratio >= self.th.get("warning", 80):
            return "danger"
        if ratio >= self.th.get("safe", 70):
            return "warning"
        return "safe"

    @staticmethod
    def _crowding_emoji(level: str) -> str:
        return {"safe": "🟢", "warning": "🟡", "danger": "🟠", "extreme": "🔴"}.get(level, "⚪")

    def _group(self, item: dict) -> str:
        fd = item.get("fund_delta", 0)
        cr = self._crowding(item.get("total_ratio", 0))
        if fd >= 5 and cr in ["safe", "warning"]:
            return "A"
        if fd >= 2:
            return "B"
        return "C"

    def _weight(self, fr: float) -> int:
        if fr >= 15:
            return self.alloc.get("fund_gt_15", 12)
        elif fr >= 8:
            return self.alloc.get("fund_8_to_15", 10)
        elif fr >= 5:
            return self.alloc.get("fund_5_to_8", 8)
        return self.alloc.get("fund_lt_5", 6)

    def build_portfolio(self, ranked: list[dict]) -> list[dict]:
        out = []
        for item in ranked:
            p = dict(item)
            p["crowding"] = self._crowding(item.get("total_ratio", 0))
            p["crowding_emoji"] = self._crowding_emoji(p["crowding"])
            p["group"] = self._group(item)
            p["weight"] = self._weight(item.get("fund_ratio", 0))
            out.append(p)
        return out

    def run(self, data: list[dict]) -> dict:
        ranked = self.rank(data)
        portfolio = self.build_portfolio(ranked)
        report = self._report(portfolio)
        return {"portfolio": portfolio, "report": report}

    @staticmethod
    def _report(portfolio: list[dict]) -> str:
        lines = ["=" * 65, "📊 五大美股巨头A股供应链 | 增量抱团Top10",
                 f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 65, ""]
        for i, p in enumerate(portfolio, 1):
            lines.append(
                f"{i:2d}. {p['crowding_emoji']} [{p['group']}] {p['name']}({p['ts_code']})\n"
                f"    链: {p.get('chain', 'N/A')} | "
                f"基金{p.get('fund_ratio', 0):.1f}%({p.get('fund_ratio_prev', 0):.1f}%) "
                f"+{p.get('fund_delta', 0):.1f}% | "
                f"机构{p.get('inst_ratio', 0):.1f}% | "
                f"合计{p.get('total_ratio', 0):.1f}% | "
                f"仓位{p['weight']}%"
            )
        a = sum(1 for p in portfolio if p["group"] == "A")
        b = sum(1 for p in portfolio if p["group"] == "B")
        c = sum(1 for p in portfolio if p["group"] == "C")
        avg_d = sum(p.get("fund_delta", 0) for p in portfolio) / max(len(portfolio), 1)
        lines.append("")
        lines.append(f"A组(核心加仓): {a}只 | B组(持续加仓): {b}只 | C组(观察): {c}只")
        lines.append(f"平均基金增量: +{avg_d:.1f}%")
        lines.append("=" * 65)
        return "\n".join(lines)


class BarkPusher:
    def __init__(self, key: str = "", server: str = "https://api.day.app"):
        self.key = key
        self.server = server.rstrip("/")

    def push(self, title: str, body: str, level: str = "active", group: str = "strategy") -> bool:
        if not self.key:
            logger.info(f"[Bark not configured] {title}: {body[:50]}...")
            return False
        url = f"{self.server}/{self.key}/{requests.utils.quote(title)}/{requests.utils.quote(body)}"
        try:
            r = requests.get(url, params={"level": level, "group": group}, timeout=10)
            if r.json().get("code") == 200:
                logger.info(f"Pushed: {title}")
                return True
            logger.error(f"Push failed: {r.json().get('message', '?')}")
        except Exception as e:
            logger.error(f"Push error: {e}")
        return False

    def push_report(self, text: str, is_adjust: bool = False):
        today = datetime.now().strftime("%m-%d")
        title = f"📊 增量抱团 | {today} | {'🔔调仓' if is_adjust else '监控'}"
        max_len = 3800
        segs = [text] if len(text) <= max_len else []
        if not segs:
            cur = ""
            for line in text.split("\n"):
                if len(cur) + len(line) + 1 > max_len:
                    segs.append(cur)
                    cur = line + "\n"
                else:
                    cur += line + "\n"
            if cur:
                segs.append(cur)
        self.push(title, segs[0], "timeSensitive" if is_adjust else "active")
        for i, seg in enumerate(segs[1:], 2):
            self.push(f"{title} (续{i})", seg, "active")

    def push_new(self, stocks: list[dict]) -> bool:
        if not stocks:
            return False
        today = datetime.now().strftime("%m-%d")
        if len(stocks) == 1:
            s = stocks[0]
            title = f"🎯 新增 | {s['name']}({s['code']})"
            body = f"股票: {s['name']} ({s['code']})\n产业链: {s['chain']}\n置信度: {s['score']:.2f}\n依据: {s['evidence'][:100]}\n已自动纳入候选池"
            return self.push(title, body, "timeSensitive")
        title = f"🎯 新增{len(stocks)}只 | {today}"
        lines = [f"发现{len(stocks)}只新标的:\n"]
        for i, s in enumerate(stocks, 1):
            lines.append(f"{i}. {s['name']}({s['code']}) - {s['chain']} [{s['score']:.2f}]")
        lines.append("\n以上已自动纳入候选池。")
        return self.push(title, "\n".join(lines), "timeSensitive")

    def test(self) -> bool:
        ok = self.push("🧪 推送测试", "配置成功！策略每日21:30自动推送，新标纳入时单独提醒。")
        if ok and self.key:
            self.push("🎯 新增 | 测试标的(000001.SZ)", "股票: 测试标的\n产业链: 英伟达-直接供应\n置信度: 0.85\n已自动纳入候选池")
        return ok


def fetch_delta(client: TushareClient, candidates: list[dict]) -> list[dict]:
    cur_p = client.current_period()
    year = int(cur_p[:4])
    md = cur_p[4:]
    periods = ["0331", "0630", "0930", "1231"]
    try:
        idx = periods.index(md)
        prev_p = f"{year - 1 if idx == 0 else year}{periods[idx - 1]}"
    except ValueError:
        prev_p = f"{year - 1}1231"
    logger.info(f"Delta: current={cur_p}, previous={prev_p}")
    results = []
    for i, c in enumerate(candidates, 1):
        code, name, chain = c["code"], c["name"], c.get("chain", "")
        try:
            d_cur = client.hold_data(code, name, cur_p)
            d_prev = client.hold_data(code, name, prev_p)
            d_cur["chain"] = chain
            d_cur["fund_ratio_prev"] = d_prev.get("fund_ratio", 0)
            d_cur["inst_ratio_prev"] = d_prev.get("inst_ratio", 0)
            if d_cur.get("fund_ratio", 0) == 0 and d_cur.get("inst_ratio", 0) == 0:
                d_cur = _fallback(d_cur, c)
            results.append(d_cur)
        except Exception as e:
            logger.warning(f"[{i}/{len(candidates)}] {name}({code}): {e}")
            results.append({
                "ts_code": code, "name": name, "chain": chain,
                "fund_ratio": 2.0, "inst_ratio": 15.0,
                "fund_ratio_prev": 0, "inst_ratio_prev": 0,
                "total_ratio": 17.0, "float_share": 0,
                "data_source": "estimated",
            })
    return results


def _fallback(data: dict, candidate: dict) -> dict:
    is_direct = "直接供应" in candidate.get("chain", "")
    fund = 5.0 if is_direct else 2.0
    inst = 25.0 if is_direct else 15.0
    data.update(fund_ratio=fund, inst_ratio=inst, total_ratio=fund + inst,
                fund_ratio_prev=0, inst_ratio_prev=0, data_source="estimated")
    return data


def pipeline(client: TushareClient, strategy: SupplyChainStrategy,
             bark: BarkPusher, discovery: DiscoveryEngine, dry: bool = False) -> dict:
    logger.info("=" * 65)
    logger.info("🚀 SUPPLY CHAIN STRATEGY — FULL RUN")
    logger.info("=" * 65)

    prev_codes = {s["code"] for s in discovery.load_pool()}
    result = {"portfolio": [], "report": ""}

    try:
        pool = discovery.run_scan()
        if not pool:
            pool = discovery.load_pool()
        new_stocks = discovery.get_new(pool, prev_codes)
        logger.info(f"Pool: {len(pool)} stocks ({len(new_stocks)} new)")
        if new_stocks and not dry and bark.key:
            bark.push_new(new_stocks)

        hold_data = fetch_delta(client, pool)
        result = strategy.run(hold_data)

        report = result["report"]
        if new_stocks:
            report += f"\n\n🎯 今日新发现{len(new_stocks)}只:\n"
            for s in new_stocks[:5]:
                report += f"   + {s['name']}({s['code']}) — {s['chain']} [{s['score']:.2f}]\n"
        print("\n" + report)

        if not dry and bark.key:
            bark.push_report(report)
            logger.info("✅ Report pushed")
        elif dry:
            logger.info("🧪 Dry-run: push skipped")
        else:
            logger.info("⚠️ Bark not configured: push skipped")

    except Exception as e:
        logger.error(f"Strategy error: {e}")
        raise
    finally:
        if not discovery.pool_path.exists():
            logger.info("Creating empty candidate_pool.json")
            discovery.save_pool([])
        logger.info(f"Candidate pool: {discovery.pool_path.absolute()}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="五大美股巨头A股供应链 — 全动态增量抱团策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python strategy.py config.yaml              # 完整运行
  python strategy.py config.yaml --test-push  # 测试推送
  python strategy.py config.yaml --dry-run    # 试运行

环境变量（GitHub Actions Secrets）:
  TUSHARE_TOKEN  Tushare Pro API Token
  BARK_KEY       Bark 推送设备 Key
  BARK_SERVER    Bark 服务器地址（可选）
        """,
    )
    parser.add_argument("config", nargs="?", default="config.yaml", help="策略配置文件路径")
    parser.add_argument("--test-push", action="store_true", help="测试Bark推送")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    cfg = load_config(args.config)
    level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        tushare_token = get_env_token("TUSHARE_TOKEN", required=not args.test_push)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)
    bark_key = get_env_token("BARK_KEY", required=False)
    bark_server = os.getenv("BARK_SERVER", "https://api.day.app").strip() or "https://api.day.app"

    client = TushareClient(tushare_token)
    strategy = SupplyChainStrategy(cfg)
    bark = BarkPusher(key=bark_key, server=bark_server)
    discovery = DiscoveryEngine(client, cfg)

    if args.test_push:
        if not bark_key:
            print("❌ BARK_KEY not set!")
            sys.exit(1)
        ok = bark.test()
        print("✅ Push test passed!" if ok else "❌ Push test failed!")
    else:
        pipeline(client, strategy, bark, discovery, dry=args.dry_run)


if __name__ == "__main__":
    main()
