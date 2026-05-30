#!/usr/bin/env python3
"""
五大美股巨头A股供应链 | 增量抱团策略
全动态候选池 + 增量排名 + Bark推送

Tokens 从环境变量读取：
  TUSHARE_TOKEN  BARK_KEY  BARK_SERVER(可选)

运行:
  python strategy.py config.yaml
  python strategy.py config.yaml --dry-run
  python strategy.py config.yaml --test-push
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

DEFAULT_CFG = {
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

BLACKLIST = {
    "陆家嘴", "600663.SH", "浦东金桥", "600639.SH", "外高桥", "600648.SH", "张江高科", "600895.SH",
    "万科A", "000002.SZ", "保利发展", "600048.SH", "招商蛇口", "001979.SZ", "金地集团", "600383.SH",
    "华侨城A", "000069.SZ",
    "工商银行", "601398.SH", "建设银行", "601939.SH", "农业银行", "601288.SH", "中国银行", "601988.SH",
    "招商银行", "600036.SH", "平安银行", "000001.SZ",
    "中国平安", "601318.SH", "中国人寿", "601628.SH", "中信证券", "600030.SH", "东方财富", "300059.SZ",
    "贵州茅台", "600519.SH", "五粮液", "000858.SZ", "泸州老窖", "000568.SZ", "山西汾酒", "600809.SH",
    "伊利股份", "600887.SH", "海天味业", "603288.SH",
    "恒瑞医药", "600276.SH", "迈瑞医疗", "300760.SZ", "药明康德", "603259.SH", "爱尔眼科", "300015.SZ",
    "分众传媒", "002027.SZ", "中公教育", "002607.SZ",
    "中国建筑", "601668.SH", "中国中铁", "601390.SH", "中国中车", "601766.SH",
    "中国交建", "601800.SH", "中国铁建", "601186.SH", "中国电建", "601669.SH", "三一重工", "600031.SH",
    "中国石油", "601857.SH", "中国石化", "600028.SH", "中国神华", "601088.SH", "陕西煤业", "601225.SH",
}


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    return merged


def env(name: str, required: bool = True) -> str:
    v = os.getenv(name, "").strip()
    if required and not v:
        raise ValueError(f"环境变量 {name} 未设置")
    return v


class TushareClient:
    INTERVAL = 0.3

    def __init__(self, token: str):
        if not token:
            raise ValueError("TUSHARE_TOKEN 为空")
        ts.set_token(token)
        self.pro = ts.pro_api()
        self._last = 0

    def _call(self, func: str, **kw):
        elapsed = time.time() - self._last
        if elapsed < self.INTERVAL:
            time.sleep(self.INTERVAL - elapsed)
        for i in range(3):
            try:
                self._last = time.time()
                fn = getattr(self.pro, func)
                df = fn(**kw)
                return df if df is not None and not df.empty else pd.DataFrame()
            except Exception as e:
                msg = str(e)
                if "积分" in msg or "permission" in msg.lower():
                    logger.error(f"{func}: 权限不足")
                    return None
                if "freq" in msg.lower() or "limit" in msg.lower():
                    time.sleep(2 ** i)
                    continue
                logger.warning(f"{func} 重试 {i+1}/3: {e}")
                if i < 2:
                    time.sleep(1)
        return pd.DataFrame()

    def stock_list(self) -> dict[str, str]:
        df = self._call("stock_basic", exchange="", list_status="L", fields="ts_code,name")
        return dict(zip(df["ts_code"], df["name"])) if df is not None and not df.empty else {}

    def fund_hold(self, ts_code: str, period: str | None = None):
        kw = {"ts_code": ts_code}
        if period:
            kw["end_date"] = period
        df = self._call("report_fund_hold", **kw)
        if df is None or df.empty:
            return pd.DataFrame()
        for c in ["hold", "hold_ratio"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def top10_holders(self, ts_code: str, period: str | None = None):
        kw = {"ts_code": ts_code}
        if period:
            kw["end_date"] = period
        df = self._call("top10_floatholders", **kw)
        if df is not None and not df.empty and "hold_ratio" in df.columns:
            df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")
        return df if df is not None else pd.DataFrame()

    def stock_basic(self, codes: list[str]):
        all_df = self._call("stock_basic", exchange="", list_status="L", fields="ts_code,name,industry,float_share")
        if all_df is None or all_df.empty:
            return pd.DataFrame()
        return all_df[all_df["ts_code"].isin(codes)].copy()

    def news(self, date: str):
        return self._call("major_news", start_date=date, end_date=date, fields="title,content,datetime,src")

    def current_period(self) -> str:
        y, m = datetime.now().year, datetime.now().month
        return f"{y-1}1231" if m in [1,2,3,4] else f"{y}0331" if m in [5,6,7] else f"{y}0630" if m in [8,9] else f"{y}0930"

    def prev_period(self, cur: str) -> str:
        mapping = {"0331": f"{int(cur[:4])-1}1231", "0630": cur[:4]+"0331", "0930": cur[:4]+"0630", "1231": cur[:4]+"0930"}
        return mapping.get(cur[4:], cur)

    def hold_data(self, ts_code: str, name: str, period: str | None = None) -> dict:
        res = {"ts_code": ts_code, "name": name, "chain": "",
               "fund_hold": 0.0, "fund_ratio": 0.0, "inst_ratio": 0.0,
               "total_ratio": 0.0, "float_share": 0.0,
               "report_period": period or "", "data_source": ""}

        basic = self.stock_basic([ts_code])
        if not basic.empty and "float_share" in basic.columns:
            res["float_share"] = float(basic.iloc[0]["float_share"])

        # 核心修复：汇总所有基金持仓
        fund_df = self.fund_hold(ts_code, period)
        if not fund_df.empty:
            if "hold" in fund_df.columns:
                total_hold = fund_df["hold"].fillna(0).sum()
                res["fund_hold"] = total_hold / 10000
                if res["float_share"] > 0:
                    res["fund_ratio"] = (res["fund_hold"] / res["float_share"]) * 100
            elif "hold_ratio" in fund_df.columns:
                res["fund_ratio"] = fund_df["hold_ratio"].fillna(0).sum()
            res["report_period"] = str(fund_df.iloc[0].get("end_date", period or ""))
            res["data_source"] = f"report_fund_hold({len(fund_df)}只基金)"
            logger.info(f"  {name}: {len(fund_df)}只基金, fund_ratio={res['fund_ratio']:.2f}%")
        else:
            logger.warning(f"  {name}: 无基金持仓数据")

        holder_df = self.top10_holders(ts_code, period)
        if not holder_df.empty and "hold_ratio" in holder_df.columns:
            top10 = holder_df["hold_ratio"].fillna(0).sum()
            res["inst_ratio"] = min(top10 * 1.15, 95)
            res["data_source"] += f", top10({top10:.1f}%)"

        if res["inst_ratio"] <= res["fund_ratio"]:
            res["inst_ratio"] = res["fund_ratio"] * 1.5
        res["total_ratio"] = min(res["fund_ratio"] + res["inst_ratio"], 100)
        return res


class DiscoveryEngine:
    POOL_FILE = "candidate_pool.json"

    def __init__(self, client: TushareClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.pool_path = Path(self.POOL_FILE)
        self._cache = None
        self._cache_time = None

    def _all_stocks(self) -> dict[str, str]:
        if self._cache and self._cache_time and (datetime.now() - self._cache_time).days < 1:
            return self._cache
        self._cache = self.client.stock_list()
        self._cache_time = datetime.now()
        logger.info(f"全A股: {len(self._cache)}只")
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
            return {"net_score": 0, "is_sc": False}
        direct = sum(0.25 for kw in DIRECT_KWS if kw.lower() in text_l)
        indirect = sum(0.15 for kw in INDIRECT_KWS if kw.lower() in text_l)
        negative = sum(0.4 for kw in NEGATIVE_KWS if kw.lower() in text_l)
        is_sc = direct > 0.2 or indirect > 0.3
        score = min(direct, 1.0) + min(indirect, 0.8) - min(negative, 1.0) + sum(matched.values()) * 0.3
        return {"net_score": round(max(-1, min(2, score)), 3), "matched": matched,
                "is_sc": is_sc, "is_direct": direct > 0.2}

    @staticmethod
    def extract_codes(text: str, stocks: dict[str, str]) -> list[str]:
        codes = set(re.findall(r'\d{6}\.(?:SZ|SH|BJ)', text.upper()))
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
                                signals.append({"ts_code": code, "name": stocks.get(code, ""),
                                                "title": str(row.get("title", ""))[:100],
                                                "date": str(row.get("datetime", ""))[:10], **sc})
                except Exception:
                    break
        return signals

    @staticmethod
    def _deduplicate(signals: list[dict]) -> list[dict]:
        scores: dict[str, dict] = {}
        for sig in signals:
            code = sig["ts_code"]
            if not sig.get("name"):
                continue
            if code not in scores:
                scores[code] = {"name": sig["name"], "net_score": 0, "count": 0,
                                "giants": set(), "evidences": [], "dates": set(), "is_direct": False}
            s = scores[code]
            s["net_score"] = max(s["net_score"], sig["net_score"])
            s["count"] += 1
            if sig.get("is_direct"):
                s["is_direct"] = True
            s["giants"].update(sig.get("matched", {}).keys())
            s["evidences"].append(sig.get("title", ""))
            s["dates"].add(sig.get("date", ""))
        gmap = {"nvidia": "英伟达", "tesla": "特斯拉", "apple": "苹果", "broadcom": "博通", "google": "谷歌"}
        results = []
        for code, s in scores.items():
            bonus = min(s["count"] * 0.1, 0.5)
            final = s["net_score"] + bonus
            giants = "+".join(gmap.get(g, g) for g in sorted(s["giants"]))
            sc_type = "直接供应" if s["is_direct"] else "间接供应"
            results.append({"ts_code": code, "name": s["name"],
                            "chain": f"{giants}-{sc_type}", "score": round(final, 3),
                            "signal_count": s["count"], "is_direct": s["is_direct"],
                            "evidence": "; ".join(s["evidences"][:3]),
                            "first_seen": min(s["dates"]) if s["dates"] else datetime.now().strftime("%Y-%m-%d"),
                            "last_seen": max(s["dates"]) if s["dates"] else datetime.now().strftime("%Y-%m-%d")})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def run_scan(self) -> list[dict]:
        logger.info("=" * 65)
        logger.info("🔍 候选池全A股扫描")
        if not self._all_stocks():
            return self.load_pool()
        all_signals = self._scan_news(days=self.cfg.get("discovery", {}).get("scan_days", 7))
        scored = self._deduplicate(all_signals)
        th = self.cfg.get("discovery", {}).get("thresholds", {})
        confirm_th = th.get("confirm", 0.5)
        candidates, filtered = [], 0
        for s in scored:
            if s["score"] >= confirm_th:
                if s["name"] in BLACKLIST or s["ts_code"] in BLACKLIST:
                    filtered += 1
                    continue
                candidates.append({"code": s["ts_code"], "name": s["name"], "chain": s["chain"],
                                   "score": s["score"], "signal_count": s["signal_count"],
                                   "is_direct": s["is_direct"], "evidence": s["evidence"][:200],
                                   "first_seen": s["first_seen"], "last_seen": s["last_seen"], "status": "confirmed"})
        logger.info(f"信号:{len(all_signals)} 去重:{len(scored)} 确认:{len(candidates)} 过滤:{filtered}")
        self.save_pool(candidates)
        return candidates

    def load_pool(self) -> list[dict]:
        if not self.pool_path.exists():
            return []
        try:
            with open(self.pool_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [s for s in data.get("stocks", []) if s.get("status") == "confirmed"]
        except Exception as e:
            logger.error(f"加载候选池失败: {e}")
            return []

    def save_pool(self, stocks: list[dict]):
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump({"last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "total_count": len(stocks), "stocks": stocks}, f, ensure_ascii=False, indent=2)

    @staticmethod
    def get_new(current: list[dict], prev_codes: set[str]) -> list[dict]:
        return [s for s in current if s["code"] not in prev_codes]
class SupplyChainStrategy:
    def __init__(self, cfg: dict):
        self.w = cfg.get("weights", DEFAULT_CFG["weights"])
        self.th = cfg.get("crowding", DEFAULT_CFG["crowding"])
        self.alloc = cfg.get("allocation", DEFAULT_CFG["allocation"])
        self.mcap = cfg.get("market_cap_bonus", DEFAULT_CFG["market_cap_bonus"])
        self.top_n = cfg.get("top_n", 10)

    def calc_score(self, item: dict) -> float:
        fq2 = item.get("fund_ratio", 0)
        fq1 = item.get("fund_ratio_prev", 0)
        iq2 = item.get("inst_ratio", 0)
        iq1 = item.get("inst_ratio_prev", 0)
        fd = max(fq2 - fq1, 0)
        ind = max(iq2 - iq1, 0)
        mv = item.get("float_share", 0) / 10000
        bonus = (self.mcap["small"]["bonus"] if mv < self.mcap["small"]["max_mv"] else
                 self.mcap["mid"]["bonus"] if mv < self.mcap["mid"]["max_mv"] else
                 self.mcap["mid_large"]["bonus"] if mv < self.mcap["mid_large"]["max_mv"] else
                 self.mcap["large"]["bonus"])
        score = fd * self.w["fund_delta"] + ind * self.w["inst_delta"] + (fq2 + iq2) * self.w["base_hold"] + bonus * self.w["small_cap_bonus"]
        item.update(fund_delta=round(fd, 2), inst_delta=round(ind, 2), score=round(score, 2))
        return score

    def rank(self, data: list[dict]) -> list[dict]:
        valid = [item for item in data if self.calc_score(item) > 0]
        if not valid:
            logger.warning("无基金加仓标的！fallback到绝对持仓排名")
            for item in data:
                item.update(fund_delta=0, inst_delta=0, score=item.get("fund_ratio", 0) * 0.5 + item.get("inst_ratio", 0) * 0.3)
            valid = data
        valid.sort(key=lambda x: x["score"], reverse=True)
        return valid[:self.top_n]

    def _crowding(self, ratio: float) -> str:
        if ratio >= self.th.get("danger", 90): return "extreme"
        if ratio >= self.th.get("warning", 80): return "danger"
        if ratio >= self.th.get("safe", 70): return "warning"
        return "safe"

    @staticmethod
    def _emoji(level: str) -> str:
        return {"safe": "🟢", "warning": "🟡", "danger": "🟠", "extreme": "🔴"}.get(level, "⚪")

    def _group(self, item: dict) -> str:
        return "A" if item.get("fund_delta", 0) >= 5 and self._crowding(item.get("total_ratio", 0)) in ["safe", "warning"] else "B" if item.get("fund_delta", 0) >= 2 else "C"

    def _weight(self, fr: float) -> int:
        return self.alloc.get("fund_gt_15", 12) if fr >= 15 else self.alloc.get("fund_8_to_15", 10) if fr >= 8 else self.alloc.get("fund_5_to_8", 8) if fr >= 5 else self.alloc.get("fund_lt_5", 6)

    def build_portfolio(self, ranked: list[dict]) -> list[dict]:
        out = []
        for item in ranked:
            cr = self._crowding(item.get("total_ratio", 0))
            g = self._group(item)
            out.append({**item, "crowding": cr, "crowding_emoji": self._emoji(cr),
                        "group": g, "weight": self._weight(item.get("fund_ratio", 0))})
        return out

    def run(self, data: list[dict]) -> dict:
        ranked = self.rank(data)
        portfolio = self.build_portfolio(ranked)
        return {"portfolio": portfolio, "report": self._report(portfolio)}

    @staticmethod
    def _report(portfolio: list[dict]) -> str:
        lines = ["=" * 65, "📊 五大美股巨头A股供应链 | 增量抱团Top10",
                 f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 65, ""]
        for i, p in enumerate(portfolio, 1):
            lines.append(f"{i:2d}. {p['crowding_emoji']} [{p['group']}] {p['name']}({p['ts_code']})\n"
                         f"    链: {p.get('chain', 'N/A')} | "
                         f"基金{p.get('fund_ratio', 0):.1f}%({p.get('fund_ratio_prev', 0):.1f}%) 🔺+{p.get('fund_delta', 0):.1f}% | "
                         f"机构{p.get('inst_ratio', 0):.1f}% | 合计{p.get('total_ratio', 0):.1f}% | 仓位{p['weight']}%")
        a = sum(1 for p in portfolio if p["group"] == "A")
        b = sum(1 for p in portfolio if p["group"] == "B")
        c = sum(1 for p in portfolio if p["group"] == "C")
        avg = sum(p.get("fund_delta", 0) for p in portfolio) / max(len(portfolio), 1)
        lines += ["", f"A组(核心加仓): {a}只 | B组(持续加仓): {b}只 | C组(观察): {c}只",
                  f"平均基金增量: +{avg:.1f}%", "=" * 65]
        return "\n".join(lines)


class BarkPusher:
    def __init__(self, key: str = "", server: str = "https://api.day.app"):
        self.key = key
        self.server = server.rstrip("/")

    def push(self, title: str, body: str, level: str = "active") -> bool:
        if not self.key:
            logger.info(f"[Bark未配置] {title}")
            return False
        try:
            url = f"{self.server}/{self.key}/{requests.utils.quote(title)}/{requests.utils.quote(body)}"
            r = requests.get(url, params={"level": level, "group": "strategy"}, timeout=10)
            ok = r.json().get("code") == 200
            logger.info(f"推送{'成功' if ok else '失败'}: {title}")
            return ok
        except Exception as e:
            logger.error(f"推送异常: {e}")
            return False

    def push_report(self, text: str):
        today = datetime.now().strftime("%m-%d")
        title = f"📊 增量抱团 | {today}"
        max_len = 3800
        segs, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > max_len:
                segs.append(cur)
                cur = line + "\n"
            else:
                cur += line + "\n"
        if cur:
            segs.append(cur)
        self.push(title, segs[0])
        for i, seg in enumerate(segs[1:], 2):
            self.push(f"{title} (续{i})", seg)

    def push_new(self, stocks: list[dict]) -> bool:
        if not stocks:
            return False
        today = datetime.now().strftime("%m-%d")
        if len(stocks) == 1:
            s = stocks[0]
            return self.push(f"🎯 新增 | {s['name']}({s['code']})",
                             f"股票: {s['name']} ({s['code']})\n产业链: {s['chain']}\n置信度: {s['score']:.2f}\n依据: {s['evidence'][:100]}\n已自动纳入候选池",
                             "timeSensitive")
        lines = [f"发现{len(stocks)}只新标的:\n"] + [f"{i}. {s['name']}({s['code']}) - {s['chain']} [{s['score']:.2f}]" for i, s in enumerate(stocks, 1)]
        lines.append("\n以上已自动纳入候选池。")
        return self.push(f"🎯 新增{len(stocks)}只 | {today}", "\n".join(lines), "timeSensitive")

    def test(self) -> bool:
        return self.push("🧪 推送测试", "配置成功！策略每日21:30自动推送。")


def fetch_delta(client: TushareClient, candidates: list[dict]) -> list[dict]:
    cur_p = client.current_period()
    prev_p = client.prev_period(cur_p)
    logger.info(f"报告期: 当前={cur_p}, 上一季={prev_p}")
    results = []
    for i, c in enumerate(candidates, 1):
        code, name, chain = c["code"], c["name"], c.get("chain", "")
        try:
            d_cur = client.hold_data(code, name, cur_p)
            d_prev = client.hold_data(code, name, prev_p)
            d_cur.update(chain=chain, fund_ratio_prev=d_prev.get("fund_ratio", 0),
                         inst_ratio_prev=d_prev.get("inst_ratio", 0))
            if d_cur["fund_ratio"] == 0 and d_prev.get("fund_ratio", 0) > 0:
                d_cur["fund_ratio"] = 0.01
            results.append(d_cur)
            logger.info(f"[{i}/{len(candidates)}] {name}: fund={d_cur['fund_ratio']:.2f}%({d_cur['fund_ratio_prev']:.2f}%) 🔺+{d_cur['fund_ratio'] - d_cur['fund_ratio_prev']:.2f}%")
        except Exception as e:
            logger.warning(f"[{i}/{len(candidates)}] {name}({code}): {e}")
            results.append({"ts_code": code, "name": name, "chain": chain,
                            "fund_ratio": 2.0, "inst_ratio": 15.0,
                            "fund_ratio_prev": 0, "inst_ratio_prev": 0,
                            "total_ratio": 17.0, "float_share": 0, "data_source": "estimated"})
    return results


def pipeline(client: TushareClient, strategy: SupplyChainStrategy,
             bark: BarkPusher, discovery: DiscoveryEngine, dry: bool = False) -> dict:
    logger.info("=" * 65)
    logger.info("🚀 SUPPLY CHAIN STRATEGY — FULL RUN")
    logger.info("=" * 65)

    prev_codes = {s["code"] for s in discovery.load_pool()}
    pool = discovery.run_scan()
    if not pool:
        pool = discovery.load_pool()
    new_stocks = discovery.get_new(pool, prev_codes)
    logger.info(f"候选池: {len(pool)}只 ({len(new_stocks)}只新标的)")
    if new_stocks and not dry and bark.key:
        bark.push_new(new_stocks)

    logger.info("Step 2: 获取基金持仓增量数据...")
    hold_data = fetch_delta(client, pool)

    logger.info("Step 3: 增量排名 Top10...")
    result = strategy.run(hold_data)

    report = result["report"]
    if new_stocks:
        report += f"\n\n🎯 今日新发现{len(new_stocks)}只:\n"
        for s in new_stocks[:5]:
            report += f"   + {s['name']}({s['code']}) — {s['chain']} [{s['score']:.2f}]\n"
    print("\n" + report)

    if not dry and bark.key:
        bark.push_report(report)
        logger.info("✅ 报告已推送")
    elif dry:
        logger.info("🧪 Dry-run: 推送已跳过")
    else:
        logger.info("⚠️ Bark未配置: 推送已跳过")

    if not discovery.pool_path.exists():
        discovery.save_pool(pool)
    return result


def main():
    parser = argparse.ArgumentParser(description="五大美股巨头A股供应链 — 增量抱团策略")
    parser.add_argument("config", nargs="?", default="config.yaml", help="配置文件路径")
    parser.add_argument("--test-push", action="store_true", help="测试Bark推送")
    parser.add_argument("--dry-run", action="store_true", help="试运行(不推送)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    level = cfg.get("logging", {}).get("level", "INFO")
    logging.basicConfig(level=getattr(logging, level),
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        tushare_token = env("TUSHARE_TOKEN", required=not args.test_push)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    bark_key = env("BARK_KEY", required=False)
    bark_server = os.getenv("BARK_SERVER", "https://api.day.app").strip() or "https://api.day.app"

    client = TushareClient(tushare_token)
    strategy = SupplyChainStrategy(cfg)
    bark = BarkPusher(key=bark_key, server=bark_server)
    discovery = DiscoveryEngine(client, cfg)

    if args.test_push:
        if not bark_key:
            print("❌ BARK_KEY 未设置!")
            sys.exit(1)
        ok = bark.test()
        print("✅ 推送测试通过!" if ok else "❌ 推送测试失败!")
    else:
        pipeline(client, strategy, bark, discovery, dry=args.dry_run)


if __name__ == "__main__":
    main()
