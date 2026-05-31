#!/usr/bin/env python3
"""
五大美股巨头A股供应链策略 —— 全动态候选池 + 增量抱团排名
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
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
    """Tushare Pro API 封装"""

    API_INTERVAL = 0.3

    # 已知的基金持仓接口名（按优先级尝试）
    FUND_APIS = ["report_fund_hold", "fund_portfolio", "fund_holdings", "inst_holdings"]

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("TUSHARE_TOKEN", "")
        if not self.token:
            raise ValueError("TUSHARE_TOKEN not found. Register at https://tushare.pro/register")
        ts.set_token(self.token)
        self.pro = ts.pro_api()
        self._last_call_time = 0

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
                if "token" in msg.lower() or "接口名" in msg:
                    logger.warning(f"[{func_name}] Not available: {msg}")
                    return None
                if "freq" in msg.lower() or "limit" in msg.lower():
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited, wait {wait}s...")
                    time.sleep(wait)
                    continue
                logger.warning(f"[{func_name}] fail (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(1)
        return pd.DataFrame()

    def get_fund_holdings(self, ts_code: str, report_period: Optional[str] = None) -> pd.DataFrame:
        params = {"ts_code": ts_code}
        if report_period:
            params["end_date"] = report_period

        for api_name in self.FUND_APIS:
            df = self._safe_call(api_name, **params)
            if df is not None and not df.empty:
                logger.info(f"  [{api_name}] success for {ts_code}")
                for col in ["fund_hold", "fund_ratio"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                if "end_date" in df.columns:
                    df = df.sort_values("end_date", ascending=False)
                return df
        return pd.DataFrame()

    def get_top10_floatholders(self, ts_code: str, report_period: Optional[str] = None) -> pd.DataFrame:
        params = {"ts_code": ts_code}
        if report_period:
            params["end_date"] = report_period
        df = self._safe_call("top10_floatholders", **params)
        if df is not None and not df.empty:
            if "hold_ratio" in df.columns:
                df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")
        return df if df is not None else pd.DataFrame()

    def get_stock_basic(self, ts_codes: List[str]) -> pd.DataFrame:
        all_stocks = self._safe_call(
            "stock_basic", exchange="", list_status="L",
            fields="ts_code,name,industry,total_share,float_share,list_date"
        )
        if all_stocks is None or all_stocks.empty:
            return pd.DataFrame()
        return all_stocks[all_stocks["ts_code"].isin(ts_codes)].copy()

    def get_stock_hold_data(self, ts_code: str, stock_name: str,
                            report_period: Optional[str] = None) -> Dict:
        result = {
            "ts_code": ts_code, "name": stock_name,
            "fund_hold": 0, "fund_ratio": 0,
            "inst_ratio": 0, "total_ratio": 0,
            "float_share": 0, "fund_count": 0,
            "report_period": report_period or "",
            "data_source": "",
        }

        basic_df = self.get_stock_basic([ts_code])
        if not basic_df.empty and "float_share" in basic_df.columns:
            result["float_share"] = float(basic_df.iloc[0]["float_share"])

        fund_df = self.get_fund_holdings(ts_code, report_period)
        if not fund_df.empty:
            latest = fund_df.iloc[0]
            result["fund_hold"] = float(latest.get("fund_hold", 0) or 0)
            if result["float_share"] > 0 and result["fund_hold"] > 0:
                result["fund_ratio"] = (result["fund_hold"] / result["float_share"]) * 100
            result["report_period"] = str(latest.get("end_date", ""))
            result["data_source"] = "report_fund_hold"

        holders_df = self.get_top10_floatholders(ts_code, report_period)
        if not holders_df.empty:
            top10_ratio = holders_df["hold_ratio"].sum() if "hold_ratio" in holders_df.columns else 0
            result["inst_ratio"] = min(top10_ratio * 1.15, 95)
            result["data_source"] += "+top10_floatholders"

        if result["inst_ratio"] <= result["fund_ratio"]:
            result["inst_ratio"] = result["fund_ratio"] * 1.5

        result["total_ratio"] = result["fund_ratio"] + result["inst_ratio"]
        return result

    def get_current_report_period(self) -> str:
        now = datetime.now()
        y, m = now.year, now.month
        if m in [1, 2, 3]:      return f"{y - 1}0930"
        elif m in [4, 5, 6, 7]: return f"{y - 1}1231"
        elif m in [8, 9, 10]:   return f"{y}0630"
        else:                   return f"{y}0930"

    def is_in_adjust_window(self) -> bool:
        now = datetime.now()
        for w_month, w_start, w_end in [(5, 1, 15), (9, 1, 15), (11, 1, 15)]:
            if now.month == w_month and w_start <= now.day <= w_end:
                return True
        return False


# ============================================================================
# 2. 全A股自动发现引擎（零硬编码）
# ============================================================================

INDUSTRY_KWS = [
    "光模块", "光器件", "光芯片", "CPO", "硅光", "800G", "1.6T",
    "PCB", "覆铜板", "CCL", "高频覆铜板", "高速PCB",
    "刻蚀设备", "薄膜设备", "清洗设备", "半导体设备", "先进封装", "Chiplet",
    "AI芯片", "GPU", "算力", "智算中心", "数据中心",
    "液冷", "浸没式液冷", "散热", "温控",
    "HBM", "DDR5", "内存接口", "存储芯片",
    "汽车电子", "智能驾驶", "激光雷达", "BMS", "热管理", "线控制动", "一体化压铸",
    "人形机器人", "谐波减速器", "行星减速器", "滚珠丝杠", "空心杯电机", "无框力矩电机",
    "高速铜缆", "DAC", "高频铜箔", "服务器", "代工", "ODM", "晶圆",
    "封测", "电机", "电池", "储能", "电力", "芯片", "集成电路",
]


class DiscoveryEngine:
    """全A股自动发现引擎 —— 候选池唯一来源，零硬编码"""

    POOL_FILE = "candidate_pool.json"

    def __init__(self, tushare_client: Optional[TushareClient] = None):
        self.ts_client = tushare_client
        self.pool_path = Path.cwd() / self.POOL_FILE
        self._stocks_cache = None
        self._cache_time = None

    def get_all_stocks(self) -> Dict[str, str]:
        if self._stocks_cache and self._cache_time and (datetime.now() - self._cache_time).days < 1:
            return self._stocks_cache
        if self.ts_client is None:
            return {}
        try:
            df = self.ts_client.pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
            if df is not None and not df.empty:
                self._stocks_cache = dict(zip(df["ts_code"], df["name"]))
                self._cache_time = datetime.now()
                logger.info(f"Loaded {len(self._stocks_cache)} A-share stocks")
                return self._stocks_cache
        except Exception as e:
            logger.error(f"Failed to fetch stock list: {e}")
        return self._stocks_cache or {}

    def scan_news(self, days: int = 3) -> List[Dict]:
        signals = []
        if self.ts_client is None:
            return signals
        all_stocks = self.get_all_stocks()
        search_kws = ["英伟达", "NVIDIA", "特斯拉", "苹果", "博通", "谷歌"]

        for keyword in search_kws:
            for d in range(days):
                date = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
                try:
                    df = self.ts_client.pro.major_news(
                        start_date=date, end_date=date,
                        fields="title,content,datetime,src"
                    )
                    if df is None or df.empty:
                        continue
                    for _, row in df.iterrows():
                        full = f"{row.get('title', '')} {row.get('content', '')}"
                        codes = self._extract_codes(full, all_stocks)
                        for code in codes:
                            signals.append({
                                "ts_code": code,
                                "name": all_stocks.get(code, ""),
                                "title": str(row.get("title", ""))[:100],
                                "source": f"news:{row.get('src', '')}",
                                "date": str(row.get("datetime", ""))[:10],
                            })
                except Exception:
                    break
        return signals

    def _extract_codes(self, text: str, all_stocks: Dict[str, str]) -> List[str]:
        codes = set()
        for m in re.finditer(r'(\d{6}\.(?:SZ|SH|BJ))', text.upper()):
            codes.add(m.group(1))
        for code, name in all_stocks.items():
            if len(name) >= 3 and name in text:
                codes.add(code)
        return list(codes)

    def _score_industry_match(self, name: str) -> float:
        """根据股票名称匹配产业链关键词打分"""
        score = 0
        for kw in INDUSTRY_KWS:
            if kw in name:
                score += 0.15
        return min(score, 0.75)

    def _infer_chain(self, name: str) -> str:
        """根据股票名称推断所属供应链"""
        chains = []
        if any(k in name for k in ["光模块", "光器件", "光芯片", "CPO", "服务器", "PCB", "液冷", "GPU", "高速铜缆"]):
            chains.append("英伟达")
        if any(k in name for k in ["精密", "玻璃", "声学", "无线", "耳机", "CIS", "韦尔"]):
            chains.append("苹果")
        if any(k in name for k in ["电池", "电机", "汽车电子", "智能驾驶", "激光雷达", "一体化压铸", "热管理"]):
            chains.append("特斯拉")
        if any(k in name for k in ["交换", "网络", "通信设备", "高速铜缆"]):
            chains.append("博通")
        if any(k in name for k in ["算力", "数据中心", "AI芯片", "智算"]):
            chains.append("谷歌")
        if not chains:
            chains.append("间接供应")
        return "+".join(chains) + "-间接供应(关键词匹配)"

    def _estimate_fund_ratio(self, score: float) -> float:
        if score >= 0.6:   return 6.0
        elif score >= 0.45: return 5.0
        elif score >= 0.3:  return 4.0
        elif score >= 0.15: return 3.0
        return 2.5

    def _estimate_inst_ratio(self, score: float) -> float:
        if score >= 0.6:   return 25.0
        elif score >= 0.45: return 22.0
        elif score >= 0.3:  return 18.0
        elif score >= 0.15: return 15.0
        return 12.0

    def run_daily_scan(self) -> List[Dict]:
        logger.info("=" * 60)
        logger.info("DiscoveryEngine: Daily Full A-Share Scan")
        logger.info("=" * 60)

        all_stocks = self.get_all_stocks()
        if not all_stocks:
            logger.error("Cannot get stock list, using cached pool")
            return self.load_pool()

        all_signals = []

        logger.info("Scanning news...")
        news_signals = self.scan_news(days=7)
        all_signals.extend(news_signals)
        logger.info(f"   News signals: {len(news_signals)}")

        # 如果新闻扫描为空，从全A股按行业关键词动态筛选
        if not all_signals:
            logger.warning("News scan empty! Filtering A-shares by industry keywords...")
            scored_stocks = []
            for code, name in all_stocks.items():
                score = self._score_industry_match(name)
                if score > 0:
                    scored_stocks.append({"ts_code": code, "name": name, "score": score})

            scored_stocks.sort(key=lambda x: x["score"], reverse=True)
            top_stocks = scored_stocks[:15]
            logger.info(f"   Keyword-filtered stocks: {len(top_stocks)}")

            for s in top_stocks:
                all_signals.append({
                    "ts_code": s["ts_code"], "name": s["name"],
                    "title": "行业关键词匹配", "source": "keyword_filter",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                })

        # 去重保留最高分
        best: Dict[str, Dict] = {}
        for sig in all_signals:
            code = sig["ts_code"]
            if not sig["name"]:
                continue
            if code not in best or len(sig.get("title", "")) > len(best[code].get("title", "")):
                best[code] = sig

        candidates = []
        for code, sig in best.items():
            score = self._score_industry_match(sig["name"])
            chain = self._infer_chain(sig["name"])

            candidates.append({
                "ts_code": code, "name": sig["name"], "chain": chain,
                "score": round(score + 0.6, 2),
                "signal_count": 1, "is_direct": False,
                "evidence": sig.get("title", "")[:100],
                "first_seen": sig.get("date", datetime.now().strftime("%Y-%m-%d")),
                "last_seen": sig.get("date", datetime.now().strftime("%Y-%m-%d")),
                "status": "confirmed",
                "est_fund": self._estimate_fund_ratio(score),
                "est_inst": self._estimate_inst_ratio(score),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        candidates = candidates[:15]
        logger.info(f"   Final candidates: {len(candidates)}")

        results = []
        for c in candidates:
            results.append({
                "code": c["ts_code"], "name": c["name"], "chain": c["chain"],
                "score": c["score"], "signal_count": c["signal_count"],
                "is_direct": c["is_direct"], "evidence": c["evidence"][:200],
                "first_seen": c["first_seen"], "last_seen": c["last_seen"],
                "status": "confirmed",
                "est_fund": c["est_fund"], "est_inst": c["est_inst"],
            })

        self.save_pool(results)
        return results

    def load_pool(self) -> List[Dict]:
        if not self.pool_path.exists():
            return []
        try:
            with open(self.pool_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stocks = data.get("stocks", [])
            logger.info(f"Loaded {len(stocks)} stocks from saved pool")
            return [s for s in stocks if s.get("status") == "confirmed"]
        except Exception as e:
            logger.error(f"Failed to load pool: {e}")
            return []

    def save_pool(self, stocks: List[Dict]):
        data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": len(stocks), "stocks": stocks,
        }
        try:
            with open(self.pool_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(stocks)} stocks to pool")
        except Exception as e:
            logger.error(f"Failed to save pool: {e}")

    def get_new_stocks(self, current_pool: List[Dict], previous_codes: Set[str]) -> List[Dict]:
        current_codes = {s["code"] for s in current_pool}
        new_codes = current_codes - previous_codes
        return [s for s in current_pool if s["code"] in new_codes]


# ============================================================================
# 3. 增量抱团策略
# ============================================================================

class SupplyChainStrategy:
    """增量抱团策略 —— 按基金持仓季度增量排名选Top10"""

    def __init__(self):
        self.w_fund_delta = 0.50
        self.w_inst_delta = 0.30
        self.w_base = 0.10
        self.w_small = 0.10
        self.th_safe = 70
        self.th_warning = 80
        self.th_danger = 90

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

        score = (
            fund_delta * self.w_fund_delta +
            inst_delta * self.w_inst_delta +
            (fund_q2 + inst_q2) * self.w_base +
            size_bonus * self.w_small
        )

        item["fund_delta"] = fund_delta
        item["inst_delta"] = inst_delta
        item["score"] = round(score, 2)
        return score

    def rank(self, hold_data: List[Dict]) -> List[Dict]:
        valid = []
        for item in hold_data:
            fund_delta = item.get("fund_ratio", 0) - item.get("fund_ratio_prev", 0)
            if fund_delta > 0:
                self.calc_score(item)
                valid.append(item)

        if not valid:
            logger.warning("No positive delta stocks! Fallback to absolute ranking.")
            for item in hold_data:
                item["fund_delta"] = 0
                item["inst_delta"] = 0
                item["score"] = item.get("fund_ratio", 0) * 0.4 + item.get("inst_ratio", 0) * 0.3
                valid.append(item)

        valid.sort(key=lambda x: x["score"], reverse=True)
        return valid[:10]

    def crowding_level(self, total_ratio: float) -> str:
        if total_ratio >= self.th_danger:   return "extreme"
        elif total_ratio >= self.th_warning: return "danger"
        elif total_ratio >= self.th_safe:    return "warning"
        return "safe"

    def crowding_emoji(self, level: str) -> str:
        return {"safe": "🟢", "warning": "🟡", "danger": "🟠", "extreme": "🔴"}.get(level, "⚪")

    def assign_group(self, item: Dict) -> str:
        fd = item.get("fund_delta", 0)
        cr = self.crowding_level(item.get("total_ratio", 0))
        if fd >= 5 and cr in ["safe", "warning"]:
            return "A"
        elif fd >= 2:
            return "B"
        return "C"

    def weight(self, item: Dict) -> int:
        fr = item.get("fund_ratio", 0)
        if fr >= 15:   return 12
        elif fr >= 8:  return 10
        elif fr >= 5:  return 8
        return 6

    def build_portfolio(self, ranked: List[Dict]) -> List[Dict]:
        portfolio = []
        for item in ranked:
            p = dict(item)
            p["crowding"] = self.crowding_level(item.get("total_ratio", 0))
            p["crowding_emoji"] = self.crowding_emoji(p["crowding"])
            p["group"] = self.assign_group(item)
            p["weight"] = self.weight(item)
            portfolio.append(p)
        return portfolio

    def run(self, hold_data: List[Dict]) -> Dict[str, Any]:
        ranked = self.rank(hold_data)
        portfolio = self.build_portfolio(ranked)
        report = self.generate_report(portfolio)
        return {"portfolio": portfolio, "report": report}

    def generate_report(self, portfolio: List[Dict]) -> str:
        if not portfolio:
            return "暂无数据\n\n请检查 Tushare token 及积分。"

        lines = []
        lines.append(f"增量抱团Top10 | {datetime.now().strftime('%m-%d %H:%M')}")
        lines.append("=" * 40)

        for i, p in enumerate(portfolio[:10], 1):
            code = p.get('ts_code', p.get('code', ''))
            lines.append(
                f"{i:2d}. {p['crowding_emoji']}[{p['group']}] {p['name']}({code})\n"
                f"    链: {p.get('chain', 'N/A')[:18]}\n"
                f"    基金: {p.get('fund_ratio', 0):.1f}%(+{p.get('fund_delta', 0):.1f}%) "
                f"机构: {p.get('inst_ratio', 0):.1f}% "
                f"仓位: {p['weight']}%"
            )

        a_count = sum(1 for p in portfolio if p["group"] == "A")
        b_count = sum(1 for p in portfolio if p["group"] == "B")
        c_count = sum(1 for p in portfolio if p["group"] == "C")
        avg_delta = sum(p.get("fund_delta", 0) for p in portfolio) / len(portfolio)

        lines.append("-" * 40)
        lines.append(f"A组: {a_count}只 | B组: {b_count}只 | C组: {c_count}只 | 平均增量: +{avg_delta:.1f}%")

        return "\n".join(lines)


# ============================================================================
# 4. Bark 推送模块（智能切分）
# ============================================================================

class BarkPusher:
    """Bark iOS 推送客户端 —— 策略报告 + 新标提醒"""

    DEFAULT_SERVER = "https://api.day.app"
    MAX_BODY_LEN = 3000

    def __init__(self, key: Optional[str] = None, server: Optional[str] = None):
        self.key = key or os.getenv("BARK_KEY", "")
        self.server = (server or os.getenv("BARK_SERVER", self.DEFAULT_SERVER)).rstrip("/")
        if not self.key:
            logger.warning("BARK_KEY not found. Push disabled.")

    def is_configured(self) -> bool:
        return bool(self.key)

    def push(self, title: str, body: str, level: str = "active",
             badge: Optional[int] = None, sound: str = "bell",
             group: Optional[str] = None) -> bool:
        if not self.is_configured():
            logger.info(f"Bark not configured. Would push: [{title}] {body[:50]}...")
            return False

        url = f"{self.server}/push"
        payload = {
            "device_key": self.key,
            "title": title,
            "body": body,
            "level": level,
        }
        if badge is not None:
            payload["badge"] = badge
        if sound:
            payload["sound"] = sound
        if group:
            payload["group"] = group

        try:
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("code") == 200:
                logger.info(f"Push sent: {title}")
                return True
            else:
                logger.error(f"Push failed: {result.get('message', 'Unknown')}")
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Push request failed: {e}")
            return False

    def push_segments(self, title: str, text: str, level: str = "active",
                      group: Optional[str] = None) -> None:
        """智能切分长文本并逐段推送"""
        if len(text) <= self.MAX_BODY_LEN:
            self.push(title=title, body=text, level=level, group=group)
            return

        lines = text.split("\n")
        segments = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > self.MAX_BODY_LEN:
                if current:
                    segments.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            segments.append(current)

        for i, segment in enumerate(segments, 1):
            seg_title = title if i == 1 else f"{title} (续{i})"
            self.push(title=seg_title, body=segment, level=level, group=group)

        logger.info(f"Pushed in {len(segments)} segment(s)")

    def push_strategy_report(self, report_text: str, is_adjust_window: bool = False):
        today_str = datetime.now().strftime("%m-%d")
        title = f"增量抱团策略 | {today_str} | {'调仓' if is_adjust_window else '监控'}"
        level = "timeSensitive" if is_adjust_window else "active"
        self.push_segments(title, report_text, level=level, group="supply-chain-strategy")

    def push_new_stocks_batch(self, new_stocks: list) -> bool:
        if not new_stocks:
            return False
        today_str = datetime.now().strftime("%m-%d")
        title = f"新增{len(new_stocks)}只标的 | {today_str}"
        body_lines = [f"【候选池扩展】发现{len(new_stocks)}只新标的:\n"]
        for i, s in enumerate(new_stocks, 1):
            code = s.get("ts_code") or s.get("code", "")
            body_lines.append(f"{i}. {s['name']}({code}) - {s['chain']}")
        body_lines.append(f"\n以上标的已纳入候选池，参与本次排名。")

        full_body = "\n".join(body_lines)
        self.push_segments(title, full_body, level="timeSensitive", group="supply-chain-strategy")
        return True

    def test_push(self) -> bool:
        return self.push(
            title="策略推送测试",
            body="推送配置成功！\n策略将在每晚21:30自动推送。",
            group="supply-chain-strategy"
        )


# ============================================================================
# 5. 主流程
# ============================================================================

def setup():
    """初始化所有组件"""
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    tushare = TushareClient()
    strategy = SupplyChainStrategy()
    bark = BarkPusher()
    discovery = DiscoveryEngine(tushare_client=tushare)
    return tushare, strategy, bark, discovery


def _fallback_data(data: Dict, candidate: Dict, is_prev: bool = False) -> Dict:
    """数据缺失时估算回退。上季设为0以显示增量。"""
    chain = candidate.get("chain", "")
    is_direct = "直接供应" in chain

    if is_prev:
        fund = 0.0
        inst = 0.0
    else:
        fund = candidate.get("est_fund", 5.0 if is_direct else 2.0)
        inst = candidate.get("est_inst", 25.0 if is_direct else 15.0)

    data["fund_ratio"] = fund
    data["inst_ratio"] = inst
    data["total_ratio"] = fund + inst
    data["fund_ratio_prev"] = 0
    data["inst_ratio_prev"] = 0
    data["data_source"] = "estimated"
    return data


def fetch_delta_data(tushare: TushareClient, candidates: List[Dict]) -> List[Dict]:
    """获取候选池每只股票的 本季+上季 持仓数据"""
    current_period = tushare.get_current_report_period()
    year = int(current_period[:4])
    md = current_period[4:]
    periods = ["0331", "0630", "0930", "1231"]
    try:
        idx = periods.index(md)
        prev_period = f"{year - 1 if idx == 0 else year}{periods[idx - 1]}"
    except ValueError:
        prev_period = f"{year - 1}1231"

    logger.info(f"Delta data: current={current_period}, previous={prev_period}")

    results = []
    for i, c in enumerate(candidates, 1):
        code, name, chain = c["code"], c["name"], c.get("chain", "")
        try:
            d_cur = tushare.get_stock_hold_data(code, name, current_period)
            d_prev = tushare.get_stock_hold_data(code, name, prev_period)

            d_cur["chain"] = chain

            cur_est = d_cur.get("fund_ratio", 0) == 0 or "estimated" in d_cur.get("data_source", "")
            prev_est = d_prev.get("fund_ratio", 0) == 0 or "estimated" in d_prev.get("data_source", "")

            if cur_est and prev_est:
                d_cur = _fallback_data(d_cur, c, is_prev=False)
                d_cur["fund_ratio_prev"] = 0
                d_cur["inst_ratio_prev"] = 0
            elif cur_est:
                d_cur = _fallback_data(d_cur, c, is_prev=False)
                d_cur["fund_ratio_prev"] = d_prev.get("fund_ratio", 0)
                d_cur["inst_ratio_prev"] = d_prev.get("inst_ratio", 0)
            elif prev_est:
                d_cur["fund_ratio_prev"] = 0
                d_cur["inst_ratio_prev"] = 0
            else:
                d_cur["fund_ratio_prev"] = d_prev.get("fund_ratio", 0)
                d_cur["inst_ratio_prev"] = d_prev.get("inst_ratio", 0)

            results.append(d_cur)
        except Exception as e:
            logger.warning(f"[{i}/{len(candidates)}] {name}({code}): {e}")
            results.append({
                "ts_code": code, "name": name, "chain": chain,
                "fund_ratio": c.get("est_fund", 3.0),
                "inst_ratio": c.get("est_inst", 15.0),
                "fund_ratio_prev": 0, "inst_ratio_prev": 0,
                "total_ratio": c.get("est_fund", 3.0) + c.get("est_inst", 15.0),
                "float_share": 0, "data_source": "estimated",
            })
    return results


def run(tushare: TushareClient, strategy: SupplyChainStrategy,
        bark: BarkPusher, discovery: DiscoveryEngine, dry_run: bool = False):
    """完整运行流程：扫描 -> 获取增量数据 -> 排名 -> 推送"""
    logger.info("=" * 65)
    logger.info("SUPPLY CHAIN STRATEGY -- FULL RUN")
    logger.info("=" * 65)

    # Step 1: 全A股扫描生成候选池
    logger.info("Step 1: Discovery -- Full A-Share Scan")
    previous_codes = {s["code"] for s in discovery.load_pool()}
    candidate_pool = discovery.run_daily_scan()
    if not candidate_pool:
        logger.error("Discovery returned empty pool! Using cached pool.")
        candidate_pool = discovery.load_pool()

    new_stocks = discovery.get_new_stocks(candidate_pool, previous_codes)
    logger.info(f"Candidate pool: {len(candidate_pool)} stocks ({len(new_stocks)} new)")

    if new_stocks and not dry_run and bark.is_configured():
        logger.info(f"Pushing {len(new_stocks)} new stocks to Bark")
        bark.push_new_stocks_batch(new_stocks)
    elif new_stocks and dry_run:
        logger.info(f"Dry-run: Would push {len(new_stocks)} new stocks")

    # Step 2: 获取增量数据
    logger.info("Step 2: Fetching delta data (current + previous quarter)")
    hold_data = fetch_delta_data(tushare, candidate_pool)

    # Step 3: 增量排名
    logger.info("Step 3: Delta Ranking -- Top10 by fund increment")
    result = strategy.run(hold_data)

    # Step 4: 报告 & 推送
    report = result["report"]
    if new_stocks:
        report += f"\n\n今日新发现{len(new_stocks)}只标的:\n"
        for s in new_stocks[:5]:
            report += f"   + {s['name']}({s['code']}) -- {s['chain']} [score:{s['score']:.2f}]\n"

    print("\n" + report)

    if not dry_run and bark.is_configured():
        bark.push_strategy_report(report)
        logger.info("Report pushed to Bark")
    elif dry_run:
        logger.info("Dry-run: Push skipped")
    else:
        logger.info("Bark not configured: Push skipped")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="五大美股巨头A股供应链 -- 全动态候选池增量抱团策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python supply_chain_strategy_merged.py              # 完整运行（扫描->排名->推送）
  python supply_chain_strategy_merged.py --test-push  # 测试Bark推送
  python supply_chain_strategy_merged.py --dry-run    # 试运行（不推送）
        """
    )
    parser.add_argument("--test-push", action="store_true", help="测试推送")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    if not os.getenv("TUSHARE_TOKEN") and not args.test_push:
        print("TUSHARE_TOKEN not set! Copy .env.example to .env and fill in.")
        sys.exit(1)

    try:
        tushare, strategy, bark, discovery = setup()
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        sys.exit(1)

    if args.test_push:
        success = bark.test_push()
        if success:
            bark.push(
                title="策略推送测试",
                body="推送配置成功！\n策略将在每晚21:30自动推送。",
                group="supply-chain-strategy"
            )
            print("All push tests passed! Check your iPhone.")
    else:
        run(tushare, strategy, bark, discovery, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
