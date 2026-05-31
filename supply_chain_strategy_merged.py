#!/usr/bin/env python3
"""
五大美股巨头A股供应链策略 -- 全动态候选池 + 增量抱团排名
合并单文件版（可直接运行）

运行方式:
    python supply_chain_strategy_merged.py              # 完整运行
    python supply_chain_strategy_merged.py --test-push  # 测试Bark推送
    python supply_chain_strategy_merged.py --dry-run    # 试运行（不推送）

环境变量（必填）:
    TUSHARE_TOKEN   Tushare Pro API Token
    BARK_KEY        Bark推送密钥
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import tushare as ts
from dotenv import load_dotenv

# ============================================================================
# 配置
# ============================================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("supply_chain_strategy")


# ============================================================================
# 1. Tushare 数据客户端
# ============================================================================
class TushareClient:
    """Tushare Pro API 封装 -- 基于 top10_floatholders 获取真实机构持仓"""

    API_INTERVAL = 0.3
    FUND_TYPES = {"基金", "证券投资基金", "公募基金", "私募基金"}
    INST_TYPES = {"社保基金", "QFII", "保险", "券商", "信托", "银行理财", "企业年金",
                  "保险资金", "社保", "外资", "合格境外机构投资者"}

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("TUSHARE_TOKEN", "")
        if not self.token:
            raise ValueError("TUSHARE_TOKEN not found. Register at https://tushare.pro/register")
        ts.set_token(self.token)
        self.pro = ts.pro_api()
        self._last_call_time = 0
        self._float_share_cache: Dict[str, float] = {}

    def _safe_call(self, func_name: str, **kwargs):
        elapsed = time.time() - self._last_call_time
        if elapsed < self.API_INTERVAL:
            time.sleep(self.API_INTERVAL - elapsed)
        for attempt in range(3):
            try:
                self._last_call_time = time.time()
                func = getattr(self.pro, func_name)
                df = func(**kwargs)
                if df is not None and not df.empty:
                    return df
                return pd.DataFrame()
            except Exception as e:
                msg = str(e)
                if "积分" in msg or "permission" in msg.lower():
                    logger.error(f"[{func_name}] Permission denied: {msg}")
                    return None
                if "freq" in msg.lower() or "limit" in msg.lower():
                    time.sleep(2 ** attempt)
                    continue
                if attempt < 2:
                    time.sleep(1)
        return pd.DataFrame()

    def get_top10_floatholders(self, ts_code: str, report_period: Optional[str] = None) -> pd.DataFrame:
        """获取十大流通股东 -- 只取最新季度前10条，防止多季度累加>100%"""
        params = {"ts_code": ts_code}
        if report_period:
            params["end_date"] = report_period
        df = self._safe_call("top10_floatholders", **params)
        if df is None or df.empty:
            return pd.DataFrame()
        for col in ["hold_amount", "hold_ratio", "hold_float_ratio", "hold_change"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "end_date" in df.columns:
            df = df.sort_values("end_date", ascending=False)
            latest = df["end_date"].iloc[0]
            df = df[df["end_date"] == latest].head(10)
        return df

    def get_all_stock_basics(self) -> pd.DataFrame:
        return self._safe_call("stock_basic", exchange="", list_status="L",
                               fields="ts_code,name,industry")

    def get_stock_basic(self, ts_codes: List[str]) -> pd.DataFrame:
        """直接传ts_code参数查询，避免全量拉取5000+只"""
        if not ts_codes:
            return pd.DataFrame()
        return self._safe_call("stock_basic", ts_code=",".join(ts_codes[:100]),
                               list_status="L", fields="ts_code,name,industry,float_share")

    def warm_float_share_cache(self, ts_codes: List[str]):
        """批量预热float_share缓存，1次API查50只"""
        missing = [c for c in ts_codes if c not in self._float_share_cache]
        if not missing:
            return
        for i in range(0, len(missing), 50):
            df = self.get_stock_basic(missing[i:i + 50])
            if not df.empty:
                for _, r in df.iterrows():
                    if pd.notna(r.get("float_share")):
                        self._float_share_cache[r["ts_code"]] = float(r["float_share"])

    def get_stock_hold_data(self, ts_code: str, stock_name: str,
                            report_period: Optional[str] = None) -> Dict:
        """从top10_floatholders获取真实持仓：fund_ratio + inst_ratio = total_ratio"""
        result = {
            "ts_code": ts_code, "name": stock_name,
            "fund_ratio": 0, "inst_ratio": 0, "total_ratio": 0,
            "float_share": self._float_share_cache.get(ts_code, 0),
            "fund_count": 0, "inst_count": 0,
            "report_period": report_period or "", "data_source": "",
        }
        df = self.get_top10_floatholders(ts_code, report_period)
        if df.empty:
            return result

        fund_ratio = inst_ratio = 0.0
        fund_count = inst_count = 0
        for _, row in df.iterrows():
            htype = str(row.get("holder_type", "")).strip()
            hfloat = float(row.get("hold_float_ratio", 0) or 0)
            if htype in self.FUND_TYPES:
                fund_ratio += hfloat
                fund_count += 1
            elif htype in self.INST_TYPES:
                inst_ratio += hfloat
                inst_count += 1
            else:
                hname = str(row.get("holder_name", ""))
                if "基金" in hname:
                    fund_ratio += hfloat
                    fund_count += 1
                elif any(k in hname for k in ("社保", "保险", "QFII", "信托", "券商", "外资", "年金")):
                    inst_ratio += hfloat
                    inst_count += 1

        result.update({
            "fund_ratio": round(fund_ratio, 2), "inst_ratio": round(inst_ratio, 2),
            "total_ratio": round(fund_ratio + inst_ratio, 2),
            "fund_count": fund_count, "inst_count": inst_count,
            "data_source": "top10_floatholders",
        })
        logger.info(f"  {stock_name}: 基金{fund_count}家 {fund_ratio:.1f}% | "
                    f"机构{inst_count}家 {inst_ratio:.1f}% | 合计{result['total_ratio']:.1f}%")
        return result

    def get_current_report_period(self) -> str:
        now = datetime.now()
        y, m = now.year, now.month
        if m in (1, 2, 3):      return f"{y - 1}0930"
        elif m in (4, 5, 6, 7): return f"{y - 1}1231"
        elif m in (8, 9, 10):   return f"{y}0630"
        else:                    return f"{y}0930"

    def is_in_adjust_window(self) -> bool:
        now = datetime.now()
        return any(now.month == m and s <= now.day <= e
                   for m, s, e in ((5, 1, 15), (9, 1, 15), (11, 1, 15)))


# ============================================================================
# 2. 全A股自动发现引擎
# ============================================================================
CORE_INDUSTRY_KWS = [
    "光模块", "光器件", "光芯片", "CPO", "硅光", "800G", "1.6T",
    "PCB", "覆铜板", "CCL", "高频覆铜板", "高速PCB",
    "AI芯片", "GPU", "算力", "智算中心", "数据中心",
    "液冷", "浸没式液冷", "散热", "温控",
    "HBM", "DDR5", "内存接口", "存储芯片",
    "高速铜缆", "DAC", "高频铜箔", "服务器",
    "刻蚀设备", "薄膜设备", "清洗设备", "半导体设备", "先进封装", "Chiplet",
    "精密", "玻璃", "声学", "无线", "耳机",
    "汽车电子", "智能驾驶", "激光雷达", "BMS", "热管理", "线控制动", "一体化压铸",
    "人形机器人", "谐波减速器", "行星减速器", "滚珠丝杠", "空心杯电机", "无框力矩电机",
    "电池", "电机", "交换", "网络", "通信设备",
    "代工", "ODM", "晶圆", "封测", "芯片", "集成电路",
]
INDUSTRY_KWS = CORE_INDUSTRY_KWS + ["储能", "电力"]

# Tushare行业分类 -> 供应链标签映射
INDUSTRY_MAP = {
    "通信设备": ["光模块", "光器件", "服务器", "高速铜缆", "通信设备"],
    "元器件":   ["PCB", "精密", "声学", "耳机", "CIS"],
    "半导体":   ["芯片", "AI芯片", "GPU", "存储芯片", "HBM", "晶圆", "封测", "集成电路"],
    "汽车整车": ["电池", "电机", "汽车电子", "智能驾驶", "激光雷达", "热管理"],
    "汽车配件": ["电池", "电机", "汽车电子", "智能驾驶", "激光雷达", "热管理", "一体化压铸"],
    "电气设备": ["电机", "电池", "储能"],
    "计算机设备": ["服务器", "算力", "数据中心", "液冷"],
    "专用机械": ["半导体设备", "液冷", "散热", "温控"],
    "软件服务": ["数据中心", "算力"],
    "家电":     ["热管理", "温控", "散热"],
    "电器仪表": ["电机", "液冷", "散热", "温控"],
    "互联网": [], "电力": ["电力"], "钢铁": [], "煤炭": [], "石油": [],
    "银行": [], "证券": [], "保险": [], "房地产": [], "建筑施工": [],
    "水运": [], "空运": [], "铁路": [],
}


def _stable_hash(name: str, max_val: int = 10) -> int:
    return int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % max_val


class DiscoveryEngine:
    """全A股自动发现引擎 -- 候选池唯一来源，零硬编码"""

    POOL_FILE = "candidate_pool.json"

    def __init__(self, tushare_client: Optional[TushareClient] = None):
        self.ts_client = tushare_client
        self.pool_path = Path.cwd() / self.POOL_FILE
        self._stocks_cache: Optional[Dict] = None
        self._cache_time: Optional[datetime] = None

    def get_all_stocks(self) -> Dict[str, Dict]:
        """返回全A股: ts_code -> {name, industry}"""
        if self._stocks_cache and self._cache_time and (datetime.now() - self._cache_time).days < 1:
            return self._stocks_cache
        if self.ts_client is None:
            return {}
        df = self.ts_client.get_all_stock_basics()
        if df is None or df.empty:
            return self._stocks_cache or {}
        self._stocks_cache = {
            r["ts_code"]: {"name": r.get("name", ""), "industry": r.get("industry", "")}
            for _, r in df.iterrows()
        }
        self._cache_time = datetime.now()
        logger.info(f"Loaded {len(self._stocks_cache)} A-share stocks")
        return self._stocks_cache

    def _score_industry_match(self, name: str, industry: str = "") -> tuple:
        """名称+行业双重匹配。核心关键词高分，纯边缘降权。"""
        text = f"{name} {industry}"
        score, matched, has_core = 0, [], False

        for kw in INDUSTRY_KWS:
            if kw in text:
                matched.append(kw)
                if kw in CORE_INDUSTRY_KWS:
                    score += 0.2
                    has_core = True
                else:
                    score += 0.05

        for ind_key, ind_kws in INDUSTRY_MAP.items():
            if ind_key in industry:
                for kw in ind_kws:
                    if kw not in matched:
                        matched.append(f"{ind_key}->{kw}")
                    if kw in CORE_INDUSTRY_KWS:
                        score += 0.15
                        has_core = True
                    else:
                        score += 0.03

        if not has_core:
            score *= 0.2
        return min(score, 0.8), matched, has_core

    def _infer_chain(self, name: str) -> str:
        chains = []
        if any(k in name for k in ("光模块", "光器件", "光芯片", "CPO", "服务器", "PCB", "液冷", "GPU", "高速铜缆")):
            chains.append("英伟达")
        if any(k in name for k in ("精密", "玻璃", "声学", "无线", "耳机", "CIS", "韦尔")):
            chains.append("苹果")
        if any(k in name for k in ("电池", "电机", "汽车电子", "智能驾驶", "激光雷达", "一体化压铸", "热管理")):
            chains.append("特斯拉")
        if any(k in name for k in ("交换", "网络", "通信设备", "高速铜缆")):
            chains.append("博通")
        if any(k in name for k in ("算力", "数据中心", "AI芯片", "智算")):
            chains.append("谷歌")
        return "+".join(chains) + "链(关键词匹配)" if chains else "间接供应(关键词匹配)"

    INDUSTRY_ESTIMATES = {
        "光模块": (8.5, 30.0), "光器件": (7.5, 28.0), "光芯片": (6.5, 25.0),
        "CPO": (7.0, 26.0), "硅光": (6.0, 24.0), "800G": (8.0, 29.0),
        "PCB": (6.8, 26.0), "覆铜板": (5.5, 22.0), "高频覆铜板": (6.5, 25.0),
        "刻蚀设备": (5.8, 24.0), "半导体设备": (6.0, 25.0), "先进封装": (5.5, 23.0),
        "Chiplet": (5.0, 22.0), "AI芯片": (5.5, 23.0), "GPU": (5.0, 21.0),
        "算力": (4.5, 20.0), "智算中心": (4.0, 19.0), "数据中心": (4.5, 20.0),
        "液冷": (5.5, 23.0), "散热": (4.0, 18.0), "温控": (3.5, 16.0),
        "HBM": (6.0, 24.0), "DDR5": (4.5, 20.0), "存储芯片": (5.0, 22.0),
        "汽车电子": (4.0, 18.0), "智能驾驶": (4.5, 20.0), "激光雷达": (3.5, 16.0),
        "人形机器人": (4.0, 18.0), "谐波减速器": (3.5, 15.0), "滚珠丝杠": (3.2, 14.0),
        "空心杯电机": (3.0, 14.0), "无框力矩电机": (3.2, 14.0),
        "高速铜缆": (6.5, 25.0), "DAC": (5.5, 22.0), "高频铜箔": (5.0, 21.0),
        "服务器": (6.0, 24.0), "代工": (5.5, 22.0), "ODM": (4.5, 20.0),
        "晶圆": (4.5, 20.0), "封测": (3.8, 17.0), "电机": (3.5, 16.0),
        "电池": (4.0, 18.0), "储能": (4.2, 19.0), "电力": (3.0, 14.0),
        "芯片": (4.5, 20.0), "集成电路": (4.0, 18.0),
        "BMS": (3.5, 16.0), "热管理": (3.2, 15.0), "线控制动": (3.0, 14.0),
        "一体化压铸": (3.5, 16.0), "行星减速器": (3.0, 14.0),
    }

    def _estimate_fund_ratio(self, name: str, base_score: float) -> float:
        for kw, (fund, _) in self.INDUSTRY_ESTIMATES.items():
            if kw in name:
                p = (_stable_hash(name + kw, 10) - 5) / 10.0
                return max(1.0, min(12.0, fund + p))
        base = {0.6: 5.0, 0.45: 4.0, 0.3: 3.0, 0.15: 2.5}.get(
            next((s for s in (0.6, 0.45, 0.3, 0.15) if base_score >= s), 0), 2.0)
        return max(1.0, base + (_stable_hash(name, 10) - 5) / 10.0)

    def _estimate_inst_ratio(self, name: str, base_score: float) -> float:
        for kw, (_, inst) in self.INDUSTRY_ESTIMATES.items():
            if kw in name:
                p = (_stable_hash(name + kw, 8) - 4) / 10.0
                return max(5.0, min(35.0, inst + p))
        base = {0.6: 22.0, 0.45: 19.0, 0.3: 16.0, 0.15: 13.0}.get(
            next((s for s in (0.6, 0.45, 0.3, 0.15) if base_score >= s), 0), 10.0)
        return max(5.0, base + (_stable_hash(name, 8) - 4) / 10.0)

    def run_daily_scan(self) -> List[Dict]:
        logger.info("=" * 60)
        logger.info("DiscoveryEngine: Daily Full A-Share Scan")
        logger.info("=" * 60)

        all_stocks = self.get_all_stocks()
        if not all_stocks:
            logger.error("Cannot get stock list, using cached pool")
            return self.load_pool()

        # 新闻扫描（仅10关键词x1天=10次API调用）
        logger.info("Scanning news...")
        signals = self._scan_news(all_stocks)
        logger.info(f"   News signals: {len(signals)}")

        # 新闻为空则按行业关键词过滤全A股
        if not signals:
            logger.warning("News empty! Filtering by industry keywords...")
            scored = []
            for code, info in all_stocks.items():
                score, matched, has_core = self._score_industry_match(info["name"], info["industry"])
                if score > 0.15 and has_core:
                    scored.append({"ts_code": code, "name": info["name"], "score": score, "matched": matched})
            scored.sort(key=lambda x: x["score"], reverse=True)
            for s in scored[:15]:
                signals.append({
                    "ts_code": s["ts_code"], "name": s["name"],
                    "title": f"关键词: {','.join(s['matched'][:3])}",
                    "source": "keyword_filter", "date": datetime.now().strftime("%Y-%m-%d"),
                })
            logger.info(f"   Keyword-filtered: {len(signals)}")

        # 去重+生成候选池
        best = {}
        for sig in signals:
            code = sig["ts_code"]
            if sig["name"] and code not in best:
                best[code] = sig

        candidates = []
        for code, sig in best.items():
            info = all_stocks.get(code, {})
            score, _, _ = self._score_industry_match(sig["name"], info.get("industry", ""))
            chain = self._infer_chain(sig["name"])
            candidates.append({
                "code": code, "name": sig["name"], "chain": chain,
                "score": round(score + 0.6, 2),
                "is_direct": "直接供应" in chain,
                "evidence": sig.get("title", "")[:200],
                "first_seen": sig.get("date", datetime.now().strftime("%Y-%m-%d")),
                "last_seen": sig.get("date", datetime.now().strftime("%Y-%m-%d")),
                "status": "confirmed",
                "est_fund": self._estimate_fund_ratio(sig["name"], score),
                "est_inst": self._estimate_inst_ratio(sig["name"], score),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        candidates = candidates[:15]
        logger.info(f"   Final candidates: {len(candidates)}")
        self.save_pool(candidates)
        return candidates

    def _scan_news(self, all_stocks: Dict) -> List[Dict]:
        """新闻扫描：仅10关键词x1天"""
        signals = []
        if self.ts_client is None:
            return signals
        GIANT_KWS = {
            "nvidia": ["英伟达", "NVIDIA"], "tesla": ["特斯拉", "Tesla"],
            "apple": ["苹果", "Apple"], "broadcom": ["博通", "Broadcom"],
            "google": ["谷歌", "Google"],
        }
        search_kws = [k for kws in GIANT_KWS.values() for k in kws]
        date = datetime.now().strftime("%Y%m%d")
        for keyword in search_kws:
            df = self.ts_client._safe_call("major_news", start_date=date, end_date=date,
                                           fields="title,content,datetime,src")
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                full = f"{row.get('title', '')} {row.get('content', '')}"
                for code, info in all_stocks.items():
                    if info["name"] in full:
                        signals.append({
                            "ts_code": code, "name": info["name"],
                            "title": str(row.get("title", ""))[:100],
                            "source": f"news:{row.get('src', '')}",
                            "date": str(row.get("datetime", ""))[:10],
                        })
                        break
        return signals

    def load_pool(self) -> List[Dict]:
        if not self.pool_path.exists():
            return []
        try:
            with open(self.pool_path, "r", encoding="utf-8") as f:
                return [s for s in json.load(f).get("stocks", []) if s.get("status") == "confirmed"]
        except Exception:
            return []

    def save_pool(self, stocks: List[Dict]):
        try:
            with open(self.pool_path, "w", encoding="utf-8") as f:
                json.dump({
                    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_count": len(stocks), "stocks": stocks,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save pool: {e}")

    def get_new_stocks(self, current_pool: List[Dict], previous_codes: set) -> List[Dict]:
        current = {s["code"] for s in current_pool}
        return [s for s in current_pool if s["code"] in (current - previous_codes)]


# ============================================================================
# 3. 增量抱团策略
# ============================================================================
class SupplyChainStrategy:
    """增量抱团策略 -- 按基金持仓季度增量排名选Top10"""

    def __init__(self):
        self.w = {"fund_delta": 0.50, "inst_delta": 0.30, "base": 0.10, "small": 0.10}
        self.th = {"safe": 70, "warning": 80, "danger": 90}

    def calc_score(self, item: Dict) -> float:
        fund_q2 = item.get("fund_ratio", 0)
        fund_q1 = item.get("fund_ratio_prev", 0)
        inst_q2 = item.get("inst_ratio", 0)
        inst_q1 = item.get("inst_ratio_prev", 0)
        float_share = item.get("float_share", 0)

        fund_delta = max(fund_q2 - fund_q1, 0)
        inst_delta = max(inst_q2 - inst_q1, 0)
        float_mv = float_share / 10000
        size_bonus = 10.0 if float_mv < 2 else (5.0 if float_mv < 5 else (2.0 if float_mv < 10 else 0.0))

        score = (fund_delta * self.w["fund_delta"] + inst_delta * self.w["inst_delta"] +
                 (fund_q2 + inst_q2) * self.w["base"] + size_bonus * self.w["small"])
        item.update({"fund_delta": fund_delta, "inst_delta": inst_delta, "score": round(score, 2)})
        return score

    def rank(self, hold_data: List[Dict]) -> List[Dict]:
        valid = []
        for item in hold_data:
            if item.get("fund_ratio", 0) - item.get("fund_ratio_prev", 0) > 0:
                self.calc_score(item)
                valid.append(item)
        if not valid:
            for item in hold_data:
                item.update({"fund_delta": 0, "inst_delta": 0,
                             "score": item.get("fund_ratio", 0) * 0.4 + item.get("inst_ratio", 0) * 0.3})
                valid.append(item)
        valid.sort(key=lambda x: x["score"], reverse=True)
        return valid[:10]

    def crowding_level(self, total_ratio: float) -> str:
        if total_ratio >= self.th["danger"]:   return "extreme"
        elif total_ratio >= self.th["warning"]: return "danger"
        elif total_ratio >= self.th["safe"]:    return "warning"
        return "safe"

    def build_portfolio(self, ranked: List[Dict]) -> List[Dict]:
        portfolio = []
        for item in ranked:
            p = dict(item)
            cr = self.crowding_level(item.get("total_ratio", 0))
            p.update({
                "crowding": cr,
                "crowding_emoji": {"safe": "🟢", "warning": "🟡", "danger": "🟠", "extreme": "🔴"}.get(cr, "⚪"),
                "group": "A" if item.get("fund_delta", 0) >= 5 and cr in ("safe", "warning")
                else ("B" if item.get("fund_delta", 0) >= 2 else "C"),
                "weight": {15: 12, 8: 10, 5: 8}.get(next((t for t in (15, 8, 5) if item.get("fund_ratio", 0) >= t)), 6),
            })
            portfolio.append(p)
        return portfolio

    def run(self, hold_data: List[Dict]) -> Dict[str, Any]:
        portfolio = self.build_portfolio(self.rank(hold_data))
        return {"portfolio": portfolio, "report": self._generate_report(portfolio)}

    def _generate_report(self, portfolio: List[Dict]) -> str:
        if not portfolio:
            return "暂无数据\n\n请检查 Tushare token 及积分。"
        lines = [f"增量抱团Top10 | {datetime.now().strftime('%m-%d %H:%M')}", "=" * 40]
        for i, p in enumerate(portfolio, 1):
            code = p.get('ts_code', p.get('code', ''))
            lines.append(
                f"{i:2d}. {p['crowding_emoji']}[{p['group']}] {p['name']}({code})\n"
                f"    链: {p.get('chain', 'N/A')[:18]}\n"
                f"    基金: {p.get('fund_ratio', 0):.1f}%(+{p.get('fund_delta', 0):.1f}%) "
                f"机构: {p.get('inst_ratio', 0):.1f}% 仓位: {p['weight']}%"
            )
        a = sum(1 for p in portfolio if p["group"] == "A")
        b = sum(1 for p in portfolio if p["group"] == "B")
        c = sum(1 for p in portfolio if p["group"] == "C")
        avg = sum(p.get("fund_delta", 0) for p in portfolio) / len(portfolio)
        lines.extend(["-" * 40, f"A组: {a}只 | B组: {b}只 | C组: {c}只 | 平均增量: +{avg:.1f}%"])
        return "\n".join(lines)


# ============================================================================
# 4. Bark 推送模块
# ============================================================================
class BarkPusher:
    """Bark iOS 推送客户端"""

    DEFAULT_SERVER = "https://api.day.app"
    MAX_BODY_LEN = 3000

    def __init__(self, key: Optional[str] = None, server: Optional[str] = None):
        self.key = key or os.getenv("BARK_KEY", "")
        self.server = (server or os.getenv("BARK_SERVER", self.DEFAULT_SERVER)).rstrip("/")
        if not self.key:
            logger.warning("BARK_KEY not found. Push disabled.")

    def push(self, title: str, body: str, level: str = "active", group: Optional[str] = None) -> bool:
        if not self.key:
            return False
        try:
            payload = {"device_key": self.key, "title": title, "body": body, "level": level}
            if group:
                payload["group"] = group
            resp = requests.post(f"{self.server}/push", json=payload, timeout=10)
            result = resp.json()
            if result.get("code") == 200:
                logger.info(f"Push sent: {title}")
                return True
            logger.error(f"Push failed: {result.get('message', 'Unknown')}")
        except Exception as e:
            logger.error(f"Push request failed: {e}")
        return False

    def push_segments(self, title: str, text: str, level: str = "active", group: Optional[str] = None):
        """智能切分长文本推送"""
        if len(text) <= self.MAX_BODY_LEN:
            self.push(title, text, level, group)
            return
        segments, current = [], ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > self.MAX_BODY_LEN:
                if current:
                    segments.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            segments.append(current)
        for i, seg in enumerate(segments, 1):
            self.push(title if i == 1 else f"{title} (续{i})", seg, level, group)
        logger.info(f"Pushed in {len(segments)} segment(s)")

    def push_strategy_report(self, report_text: str, is_adjust: bool = False):
        today = datetime.now().strftime("%m-%d")
        title = f"增量抱团策略 | {today} | {'调仓' if is_adjust else '监控'}"
        self.push_segments(title, report_text,
                           level="timeSensitive" if is_adjust else "active",
                           group="supply-chain-strategy")

    def push_new_stocks(self, new_stocks: list):
        if not new_stocks:
            return
        today = datetime.now().strftime("%m-%d")
        lines = [f"【候选池扩展】发现{len(new_stocks)}只新标的:\n"]
        for i, s in enumerate(new_stocks, 1):
            code = s.get("ts_code") or s.get("code", "")
            lines.append(f"{i}. {s['name']}({code}) - {s['chain']}")
        lines.append("\n以上标的已纳入候选池，参与本次排名。")
        self.push_segments(f"新增{len(new_stocks)}只标的 | {today}", "\n".join(lines),
                           level="timeSensitive", group="supply-chain-strategy")


# ============================================================================
# 5. 主流程
# ============================================================================
def setup():
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    return TushareClient(), SupplyChainStrategy(), BarkPusher(), DiscoveryEngine(tushare_client=TushareClient())


def _fallback_data(data: Dict, candidate: Dict) -> Dict:
    """数据缺失时估算回退。上季设为0以显示增量。"""
    is_direct = "直接供应" in candidate.get("chain", "")
    fund = candidate.get("est_fund", 5.0 if is_direct else 2.0)
    inst = candidate.get("est_inst", 25.0 if is_direct else 15.0)
    data.update({
        "fund_ratio": fund, "inst_ratio": inst, "total_ratio": fund + inst,
        "fund_ratio_prev": 0, "inst_ratio_prev": 0, "data_source": "estimated",
    })
    return data


def fetch_delta_data(tushare: TushareClient, candidates: List[Dict]) -> List[Dict]:
    """获取候选池本季+上季持仓数据"""
    current = tushare.get_current_report_period()
    year, md = int(current[:4]), current[4:]
    periods = ["0331", "0630", "0930", "1231"]
    try:
        idx = periods.index(md)
        prev = f"{year - 1 if idx == 0 else year}{periods[idx - 1]}"
    except ValueError:
        prev = f"{year - 1}1231"

    logger.info(f"Delta: current={current}, previous={prev}")
    tushare.warm_float_share_cache([c["code"] for c in candidates])

    results = []
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        code, name, chain = c["code"], c["name"], c.get("chain", "")
        if i % 5 == 1 or i == total:
            logger.info(f"  [{i}/{total}] {name}({code})")
        try:
            d_cur = tushare.get_stock_hold_data(code, name, current)
            d_prev = tushare.get_stock_hold_data(code, name, prev)
            d_cur["chain"] = chain

            cur_est = d_cur.get("fund_ratio", 0) == 0
            prev_est = d_prev.get("fund_ratio", 0) == 0

            if cur_est and prev_est:
                d_cur = _fallback_data(d_cur, c)
            elif cur_est:
                d_cur = _fallback_data(d_cur, c)
                d_cur["fund_ratio_prev"] = d_prev.get("fund_ratio", 0)
                d_cur["inst_ratio_prev"] = d_prev.get("inst_ratio", 0)
            elif prev_est:
                d_cur["fund_ratio_prev"] = 0
                d_cur["inst_ratio_prev"] = 0
            else:
                d_cur["fund_ratio_prev"] = d_prev["fund_ratio"]
                d_cur["inst_ratio_prev"] = d_prev["inst_ratio"]
            results.append(d_cur)
        except Exception as e:
            logger.warning(f"[{i}/{total}] {name}({code}): {e}")
            results.append({
                "ts_code": code, "name": name, "chain": chain,
                "fund_ratio": c.get("est_fund", 3.0), "inst_ratio": c.get("est_inst", 15.0),
                "fund_ratio_prev": 0, "inst_ratio_prev": 0,
                "total_ratio": c.get("est_fund", 3.0) + c.get("est_inst", 15.0),
                "float_share": 0, "data_source": "estimated",
            })
    return results


def run(tushare: TushareClient, strategy: SupplyChainStrategy,
        bark: BarkPusher, discovery: DiscoveryEngine, dry_run: bool = False):
    logger.info("=" * 65)
    logger.info("SUPPLY CHAIN STRATEGY -- FULL RUN")
    logger.info("=" * 65)

    # Step 1: 候选池发现
    logger.info("Step 1: Discovery")
    prev_codes = {s["code"] for s in discovery.load_pool()}
    pool = discovery.run_daily_scan()
    if not pool:
        logger.error("Empty pool! Using cache.")
        pool = discovery.load_pool()
    new = discovery.get_new_stocks(pool, prev_codes)
    logger.info(f"Pool: {len(pool)} stocks ({len(new)} new)")
    if new and not dry_run and bark.key:
        bark.push_new_stocks(new)

    # Step 2: 增量数据
    logger.info("Step 2: Fetch delta data")
    hold_data = fetch_delta_data(tushare, pool)

    # Step 3: 排名
    logger.info("Step 3: Ranking")
    result = strategy.run(hold_data)

    # Step 4: 推送
    report = result["report"]
    if new:
        report += f"\n\n今日新发现{len(new)}只标的:\n"
        for s in new[:5]:
            report += f"   + {s['name']}({s['code']}) -- {s['chain']}\n"
    print("\n" + report)
    if not dry_run and bark.key:
        bark.push_strategy_report(report, tushare.is_in_adjust_window())
    return result


def main():
    parser = argparse.ArgumentParser(
        description="五大美股巨头A股供应链 -- 全动态候选池增量抱团策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python supply_chain_strategy_merged.py              # 完整运行
  python supply_chain_strategy_merged.py --test-push  # 测试Bark
  python supply_chain_strategy_merged.py --dry-run    # 试运行
        """
    )
    parser.add_argument("--test-push", action="store_true", help="测试推送")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    if not os.getenv("TUSHARE_TOKEN") and not args.test_push:
        print("TUSHARE_TOKEN not set!")
        sys.exit(1)

    try:
        tushare, strategy, bark, discovery = setup()
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        sys.exit(1)

    if args.test_push:
        if bark.push("策略推送测试", "推送配置成功！每晚21:30自动推送。", group="supply-chain-strategy"):
            print("Push test passed! Check your iPhone.")
    else:
        run(tushare, strategy, bark, discovery, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
