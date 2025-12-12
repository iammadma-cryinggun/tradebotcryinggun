from dotenv import load_dotenv
load_dotenv()

# 🔧 调试：打印所有相关环境变量
print("=== 环境变量调试 ===")
env_vars = dict(os.environ)
for key in env_vars:
    if 'BINANCE' in key.upper() or 'TELEGRAM' in key.upper():
        value = env_vars[key]
        masked = value[:3] + "***" if value else "空"
        print(f"{key}: {masked}")
print("===================")
# -*- coding: utf-8 -*-
import ccxt
import time
import sys
import threading
from datetime import datetime, timedelta
import requests
import os
import json
import csv
import pandas as pd
from pathlib import Path
from collections import deque, defaultdict
import traceback
from dotenv import load_dotenv  # 🔧 新增

# 🔧 加载环境变量（在代码最前面）
load_dotenv()

# ==================== 1. 全局配置 (V15.7 放宽止损版) ====================

# 🚨 实盘开关: False=模拟, True=真金白银交易
LIVE_TRADING = False

# [策略开关]
ENABLE_FUNDING_FILTER = True      # 资金费率过滤器开关
ENABLE_LOGGING = True             # CSV日志记录开关
ENABLE_GRADED_STOP_LOSS = True    # 分级硬止损开关

# [资金管理]
LEVERAGE = 10                      # 降低到8倍杠杆
MARGIN_PER_TRADE = 30.0           # 单笔 20 U (降低风险)
BALANCE_BUFFER_RATIO = 0.1       # 保留 15% 余额不动

# [止损/止盈] - 放宽止损，提高成功率
STOP_LOSS_ROE = -0.12             # -12% 最终硬止损 (放宽)
TP_ROE_1 = 0.04                   # 4% TP1 (降低门槛)
TP_ROE_2 = 0.12                   # 12% TP2
TP_ROE_3 = 0.25                   # 25% TP3

PARTIAL_CLOSE_RATIO_1 = 0.30
PARTIAL_CLOSE_RATIO_2 = 0.40      # TP2增加减仓比例
PARTIAL_CLOSE_RATIO_3 = 1.00

# [风控]
BREAKEVEN_TRIGGER_ROE = 0.06      # 6% 触发保本 (降低)
POST_TRADE_COOLDOWN = 180         # 开单后冷却 3分钟

# 🆕 放宽的分级硬止损参数
GRADED_STOP_LEVELS = [
    {'roe_threshold': -0.06, 'close_ratio': 0.25, 'name': "一级止损(-6%)"},
    {'roe_threshold': -0.09, 'close_ratio': 0.40, 'name': "二级止损(-9%)"},
    {'roe_threshold': -0.12, 'close_ratio': 1.00, 'name': "强制平仓(-12%)"}
]

# 🆕 资金费率过滤 (稍微放宽)
MAX_FUNDING_RATE_LONG = 0.0015    # 做多时最大允许费率 0.15%
MIN_FUNDING_RATE_SHORT = -0.0015  # 做空时最小允许费率 -0.15%

# 🟢 [信号阈值] - 保持原有设置
BURST_1M_THRESHOLD = 0.006        # 1分钟波动 > 0.6%
MIN_VOL_USDT = 10000000           # 1000万U 以上活跃币
VOL_MULTIPLIER = 2.5              # 量能放大 2.5倍

# 🟢 [指标阈值]
MAX_COO_ENTRY = 85                # COO > 85 不追多 (稍微放宽)
MIN_COO_ENTRY = -85               # COO < -85 不追空 (稍微放宽)

# [扫描参数]
SCAN_TOP_N = 30                   # 扫描前30名
SCAN_INTERVAL = 2                 # 2秒一轮
PRICE_SNAPSHOT_INTERVAL = 2

# ==================== 日志配置 ====================
BASE_DIR = r"/app"  # 🔧 修改为Railway/Koyeb路径
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")

TRADES_LOG = os.path.join(LOG_DIR, "trades.csv")
SIGNALS_LOG = os.path.join(LOG_DIR, "signals.csv")
DAILY_LOG = os.path.join(LOG_DIR, "daily_summary.csv")
DATA_FILE = os.path.join(DATA_DIR, "positions_v15_7.json")

# ==================== 密钥配置 ====================
# 🔧 从环境变量读取（不是硬编码！）
API_KEY = os.getenv("BINANCE_API_KEY", "")  # ✅ 安全方式
SECRET_KEY = os.getenv("BINANCE_API_SECRET", "")  # ✅ 安全方式
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # ✅ 安全方式
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # ✅ 安全方式

# 🔧 安全检查
if not API_KEY or not SECRET_KEY:
    print("❌ 错误：请在环境变量中设置 BINANCE_API_KEY 和 BINANCE_API_SECRET")
    print("在 .env 文件中添加：")
    print("BINANCE_API_KEY=你的密钥")
    print("BINANCE_API_SECRET=你的密钥")
    sys.exit(1)

# Proxy配置（可选）
USE_PROXY = False  # 🔧 在云服务器上关闭代理
PROXY_PORT = 15236
PROXIES = {'http': f'http://127.0.0.1:{PROXY_PORT}', 'https': f'http://127.0.0.1:{PROXY_PORT}'}
HEADERS = {"User-Agent": "Mozilla/5.0"}
# ==================== 2. 极速快照系统 ====================
price_history = defaultdict(lambda: deque(maxlen=60)) 
last_snapshot_time = 0
running_flag = True 
trade_balance = 100.0

def background_price_snapshot():
    """后台线程：极速获取价格"""
    global last_snapshot_time
    while running_flag:
        try:
            now = time.time()
            if now - last_snapshot_time < PRICE_SNAPSHOT_INTERVAL:
                time.sleep(0.1)
                continue

            try:
                tickers = exchange.fetch_tickers()
            except:
                time.sleep(1)
                continue

            for symbol, ticker in tickers.items():
                if 'USDT:USDT' not in symbol: continue
                vol = float(ticker.get('quoteVolume', 0) or 0)
                if vol < MIN_VOL_USDT: continue
                
                price = float(ticker['last'])
                price_history[symbol].append((now, price))

            last_snapshot_time = now
            time.sleep(0.5) 
        except Exception:
            time.sleep(2)

# ==================== 3. CSV日志系统 ====================

def init_log_system():
    """初始化日志目录和CSV文件"""
    try:
        # 创建日志和数据目录
        for directory in [LOG_DIR, DATA_DIR]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                print(f"📁 创建目录: {directory}")
        
        # 交易日志表头
        trades_header = [
            'timestamp', 'symbol', 'side', 'entry_price', 'exit_price',
            'quantity', 'roe_pct', 'pnl_usdt', 'hold_time_sec',
            'open_reason', 'close_reason', 'funding_rate', 'oi_change',
            'vol_ratio', 'max_roe', 'tp_level', 'strategy_version'
        ]
        
        # 信号日志表头
        signals_header = [
            'timestamp', 'symbol', 'price', 'price_change_1m', 'vol_ratio',
            'coo_value', 'trend_direction', 'funding_rate', 'oi_status',
            'signal_strength', 'action', 'filter_reason', 'technical_ok',
            'funding_ok', 'oi_ok', 'market_condition'
        ]
        
        # 创建文件并写入表头（如果文件不存在）
        for filepath, header in [(TRADES_LOG, trades_header), (SIGNALS_LOG, signals_header)]:
            if not os.path.exists(filepath):
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(header)
                print(f"📝 创建日志文件: {filepath}")
        
        return True
    except Exception as e:
        print(f"❌ 初始化日志系统失败: {e}")
        return False

def log_trade(symbol, side, entry_price, exit_price, quantity, 
              roe_pct, pnl_usdt, hold_time, open_reason, close_reason,
              funding_rate=0.0, oi_change=0.0, vol_ratio=1.0, max_roe=0.0, tp_level=0):
    """记录单笔交易到CSV"""
    
    if not ENABLE_LOGGING:
        return
    
    timestamp = datetime.now().strftime('%Y-%m-d %H:%M:%S')
    
    trade_data = [
        timestamp, symbol, side, entry_price, exit_price,
        quantity, roe_pct, pnl_usdt, hold_time,
        open_reason, close_reason, funding_rate, oi_change,
        vol_ratio, max_roe, tp_level, 'V15.7'
    ]
    
    try:
        with open(TRADES_LOG, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(trade_data)
        
        print(f"📝 记录交易: {symbol} | 持仓:{hold_time}s | 收益:{pnl_usdt:.2f}U | 原因:{close_reason}")
    except Exception as e:
        print(f"❌ 记录交易日志失败: {e}")

def log_signal(symbol, price, price_change_1m=0.0, vol_ratio=1.0, coo_value=0.0, 
               trend_direction='neutral', funding_rate=0.0, oi_status='unknown',
               signal_strength='MEDIUM', action='ANALYZED', filter_reason='', 
               technical_ok=True, funding_ok=True, oi_ok=True, market_condition='NORMAL'):
    """记录所有分析过的信号"""
    
    if not ENABLE_LOGGING:
        return
    
    timestamp = datetime.now().strftime('%Y-%m-d %H:%M:%S')
    
    signal_data = [
        timestamp, symbol, price, price_change_1m, vol_ratio,
        coo_value, trend_direction, funding_rate, oi_status,
        signal_strength, action, filter_reason, technical_ok,
        funding_ok, oi_ok, market_condition
    ]
    
    try:
        with open(SIGNALS_LOG, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(signal_data)
    except Exception as e:
        print(f"❌ 记录信号日志失败: {e}")

# ==================== 4. 基础组件 ====================
simulated_positions = {}
cooldown = {}

def save_data():
    try:
        data = {'balance': trade_balance, 'positions': simulated_positions, 'cooldown': cooldown}
        with open(DATA_FILE, 'w') as f: json.dump(data, f, indent=4)
    except: pass

def load_data():
    global trade_balance, simulated_positions, cooldown
    if not os.path.exists(DATA_FILE): return
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
            trade_balance = data.get('balance', 100.0)
            simulated_positions = data.get('positions', {})
            cooldown = data.get('cooldown', {})
    except: pass

# ==================== 5. 简化资金费率检查 ====================
funding_cache = {}
funding_cache_time = {}

def check_funding_rate_simple(symbol, side):
    """最简资金费率检查"""
    if not ENABLE_FUNDING_FILTER:
        return True, 0.0, "过滤器已关闭"
    
    try:
        now = time.time()
        
        # 缓存检查（每3分钟）
        if symbol in funding_cache and symbol in funding_cache_time:
            if now - funding_cache_time[symbol] < 180:  # 3分钟缓存
                current_rate = funding_cache[symbol]
            else:
                funding_data = exchange.fetch_funding_rate(symbol)
                current_rate = funding_data['fundingRate']
                funding_cache[symbol] = current_rate
                funding_cache_time[symbol] = now
        else:
            funding_data = exchange.fetch_funding_rate(symbol)
            current_rate = funding_data['fundingRate']
            funding_cache[symbol] = current_rate
            funding_cache_time[symbol] = now
        
        rate_percent = current_rate * 100
        
        # 做多检查
        if side == 'buy' and current_rate > MAX_FUNDING_RATE_LONG:
            return False, current_rate, f"费率{rate_percent:.3f}%过高"
        
        # 做空检查
        elif side == 'sell' and current_rate < MIN_FUNDING_RATE_SHORT:
            return False, current_rate, f"费率{rate_percent:.3f}%过低"
        
        return True, current_rate, f"费率{rate_percent:.3f}%正常"
    
    except Exception as e:
        print(f"⚠️  获取{symbol}费率失败: {e}")
        return True, 0.0, "费率获取失败"

# ==================== 6. 消息模块 ====================

def _send_telegram_thread(msg):
    if not TELEGRAM_BOT_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5, proxies=PROXIES)
    except: pass

def send_telegram(msg):
    threading.Thread(target=_send_telegram_thread, args=(msg,)).start()

def telegram_listener():
    global running_flag, ENABLE_FUNDING_FILTER, ENABLE_LOGGING
    last_update_id = 0
    if not TELEGRAM_BOT_TOKEN: return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    
    while running_flag:
        try:
            url = f"{base_url}/getUpdates?offset={last_update_id + 1}&timeout=10"
            res = requests.get(url, timeout=15, proxies=PROXIES if USE_PROXY else None)
            if res.status_code == 200:
                for update in res.json().get('result', []):
                    last_update_id = update['update_id']
                    msg = update.get('message', {})
                    if not msg or str(msg.get('chat',{}).get('id')) != TELEGRAM_CHAT_ID: continue
                    text = msg.get('text', '').strip().lower()
                    
                    if text in ['/balance', '余额']:
                        send_telegram(f"💰 余额: `{trade_balance:.2f} U` | 持仓: `{len(simulated_positions)}`")
                    elif text in ['/filter', '过滤']:
                        status = "🟢 开启" if ENABLE_FUNDING_FILTER else "🔴 关闭"
                        send_telegram(f"📊 **过滤器状态**\n资金费率过滤: {status}")
                    elif text in ['/log', '日志']:
                        status = "🟢 开启" if ENABLE_LOGGING else "🔴 关闭"
                        send_telegram(f"📝 **日志状态**\nCSV记录: {status}\n路径: {LOG_DIR}")
                    elif text in ['/pos', '持仓']:
                        if not simulated_positions: send_telegram("🟢 空仓")
                        else:
                            m = "📊 **持仓详情**\n"
                            for s, p in simulated_positions.items():
                                m += f"{'🟢多' if p['side']=='buy' else '🔴空'} `{s.split(':')[0]}` @ {p['entry']:.4f}\n"
                            send_telegram(m)
        except: time.sleep(2)
        time.sleep(0.5)

# ==================== 7. 完整指标库 (COO) ====================

def calculate_rsi(closes, period=14):
    """计算RSI指标，带防零保护"""
    if len(closes) < period + 1: 
        return 50.0
    
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i-1]
        if chg > 0: 
            gains.append(chg)
            losses.append(0)
        else: 
            gains.append(0)
            losses.append(abs(chg))
    
    # 防零保护
    if len(gains) < period or len(losses) < period:
        return 50.0
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_stoch_k(highs, lows, closes, k_period=14):
    """计算随机指标K值，带防零保护"""
    if len(closes) < k_period: 
        return 50.0
    
    ll = [min(lows[i:i+k_period]) for i in range(len(closes)-k_period+1)]
    hh = [max(highs[i:i+k_period]) for i in range(len(closes)-k_period+1)]
    
    k_vals = []
    for i in range(len(ll)):
        div = hh[i] - ll[i]
        if div == 0:
            k = 50.0
        else:
            k = 100 * ((closes[i+k_period-1] - ll[i]) / div)
            # 限制在0-100范围内
            k = max(0, min(100, k))
        k_vals.append(k)
    
    return k_vals[-1] if k_vals else 50.0

def calculate_cci(highs, lows, closes, period=14):
    """计算CCI指标，带防零保护"""
    if len(closes) < period: 
        return 0.0
    
    # 计算典型价格
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    
    if len(tp) < period:
        return 0.0
    
    sma = sum(tp[-period:]) / period
    
    # 计算平均偏差
    deviations = [abs(p - sma) for p in tp[-period:]]
    if len(deviations) < period:
        return 0.0
    
    md = sum(deviations) / period
    
    if md == 0:
        return 0.0
    
    return (tp[-1] - sma) / (0.015 * md)

def calculate_coo(highs, lows, closes):
    """计算COO综合指标，带异常处理"""
    try:
        # 获取三个子指标
        rsi = calculate_rsi(closes, 14)
        stoch = calculate_stoch_k(highs, lows, closes, 14)
        cci = calculate_cci(highs, lows, closes, 14)
        
        # 归一化处理
        # RSI通常范围30-70，归一化到-100到100
        norm_rsi = max(-100, min(100, 2.5 * (rsi - 50)))
        
        # Stoch通常范围20-80，归一化到-100到100
        norm_stoch = max(-100, min(100, 3.33 * (stoch - 50)))
        
        # CCI通常范围-100到100，归一化到-100到100
        norm_cci = max(-100, min(100, cci))
        
        # 计算加权平均
        coo_value = (norm_rsi + norm_stoch + norm_cci) / 3.0
        
        # 限制最终范围
        coo_value = max(-100, min(100, coo_value))
        
        return coo_value
    except Exception as e:
        print(f"⚠️  COO计算异常: {e}")
        return 0.0

# ==================== 8. 核心分析逻辑 ====================

def get_fast_movers():
    """获取快速变动币种"""
    cands = []
    now = time.time()
    cutoff = now - 60 
    
    for s, h in price_history.items():
        if len(h) < 5: 
            continue
        
        # 找到1分钟前的价格
        start_p = None
        for ts, p in h:
            if ts >= cutoff:
                start_p = p
                break
        
        if not start_p: 
            continue
        
        curr = h[-1][1]
        
        # 防零保护：确保start_p不为零
        if start_p == 0:
            continue
        
        chg = (curr - start_p) / start_p
        
        if abs(chg) >= BURST_1M_THRESHOLD: 
            cands.append((s, chg))
    
    # 按变化幅度排序
    cands.sort(key=lambda x: abs(x[1]), reverse=True)
    return cands[:SCAN_TOP_N]

def safe_calculate_change(current_price, previous_price):
    """安全计算价格变化百分比"""
    if previous_price == 0:
        return 0.0
    return (current_price - previous_price) / previous_price

def safe_calculate_vol_ratio(current_volume, historical_volumes):
    """安全计算成交量比率"""
    if not historical_volumes or len(historical_volumes) == 0:
        return 1.0
    
    # 过滤掉零值
    valid_volumes = [v for v in historical_volumes if v > 0]
    
    if not valid_volumes:
        return 1.0
    
    avg_volume = sum(valid_volumes) / len(valid_volumes)
    
    if avg_volume == 0:
        return 1.0
    
    return current_volume / avg_volume

def open_position(symbol, price, side, strategy, funding_info="", 
                  vol_ratio=1.0, funding_rate=0.0, price_change_1m=0.0, coo_value=0.0):
    """开仓函数 - 记录分级止损触发状态"""
    global trade_balance
    
    if len(simulated_positions) >= 3: 
        return
    
    # 计算开仓数量
    if price == 0:
        print(f"⚠️  {symbol} 价格为零，跳过开仓")
        return
    
    amount = (MARGIN_PER_TRADE * LEVERAGE) / price
    oid = 'SIM'

    if LIVE_TRADING:
        try:
            bal = exchange.fetch_balance()['free']['USDT']
            if bal < MARGIN_PER_TRADE: 
                return
            
            order = exchange.create_order(symbol, 'market', side, amount, params={'leverage': LEVERAGE})
            oid = order['id']
        except Exception as e:
            print(f"❌ 开仓失败: {e}")
            send_telegram(f"❌ 开仓失败 `{symbol}`: {e}")
            return

    # 初始化分级止损触发记录
    graded_stop_triggered = {}
    for i, level in enumerate(GRADED_STOP_LEVELS):
        graded_stop_triggered[f'level_{i}'] = False

    # 记录开仓时的市场状态
    open_time = time.time()
    trend_direction = 'up' if side == 'buy' else 'down'
    
    simulated_positions[symbol] = {
        'entry': price, 
        'side': side, 
        'amount': amount, 
        'open_time': open_time,
        'log_open_time': open_time,
        'log_entry_price': price,
        'log_open_reason': strategy,
        'log_funding_rate': funding_rate,
        'log_vol_ratio': vol_ratio,
        'max_roe': -1.0, 
        'tp_level': 0, 
        'closed_amount': 0, 
        'current_stop_loss': STOP_LOSS_ROE,
        'highest_roe': -1.0,
        'order_id': oid,
        'graded_stop_triggered': graded_stop_triggered,  # 🆕 分级止损触发记录
        'full_stop_triggered': False  # 🆕 完全止损标记
    }
    
    cooldown[symbol] = time.time()
    save_data()
    
    # 记录信号日志
    log_signal(
        symbol=symbol,
        price=price,
        price_change_1m=price_change_1m,
        vol_ratio=vol_ratio,
        coo_value=coo_value,
        trend_direction=trend_direction,
        funding_rate=funding_rate,
        signal_strength='HIGH' if vol_ratio > 3.0 else 'MEDIUM',
        action='OPENED',
        market_condition='BULLISH' if side == 'buy' else 'BEARISH'
    )
    
    icon = "⚡" if side == 'buy' else "🩸"
    funding_text = f" | {funding_info}" if funding_info else ""
    
    # 显示分级止损信息
    stop_text = " | 分级止损: "
    for level in GRADED_STOP_LEVELS:
        stop_text += f"{level['name']} "
    
    print(f"\n{icon} [开仓] {symbol} {side} @ {price:.4f}{stop_text}{funding_text} | {strategy}")
    send_telegram(f"{icon} **趋势开仓**\n`{symbol}` {side}\n价格: {price:.4f}\n理由: {strategy}{stop_text}{funding_text}")

def analyze_and_trade(symbol):
    """分析交易对并执行交易"""
    try:
        # 检查冷却时间
        if symbol in cooldown and time.time() - cooldown[symbol] < POST_TRADE_COOLDOWN: 
            return
        
        # 1. 获取K线数据
        ohlcv_1m = exchange.fetch_ohlcv(symbol, '1m', limit=30)
        ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=30)
        
        # 数据量检查
        if len(ohlcv_1m) < 20 or len(ohlcv_5m) < 20:
            print(f"   ⚠️  {symbol} 数据不足，跳过分析")
            return
        
        # 提取价格和成交量数据
        closes_1m = [x[4] for x in ohlcv_1m]
        volumes_1m = [x[5] for x in ohlcv_1m]
        highs_1m = [x[2] for x in ohlcv_1m]
        lows_1m = [x[3] for x in ohlcv_1m]
        
        # 防零保护：检查价格数据
        if any(price <= 0 for price in closes_1m):
            print(f"   ⚠️  {symbol} 存在零或负价格，跳过分析")
            log_signal(
                symbol=symbol,
                price=closes_1m[-1] if closes_1m else 0,
                action='ERROR',
                filter_reason='存在零或负价格',
                technical_ok=False
            )
            return
        
        current_price = closes_1m[-1]
        current_volume = volumes_1m[-1] if volumes_1m else 0
        
        closes_5m = [x[4] for x in ohlcv_5m]
        
        # 计算MA25 (5分钟级别)
        if len(closes_5m) >= 25:
            ma_5m = sum(closes_5m[-25:]) / 25
        else:
            ma_5m = sum(closes_5m) / len(closes_5m) if closes_5m else current_price
        
        # 计算技术指标
        coo_value = calculate_coo(highs_1m, lows_1m, closes_1m)
        
        # 安全计算1分钟价格变化
        if len(closes_1m) >= 2 and closes_1m[-2] > 0:
            price_change_1m = safe_calculate_change(current_price, closes_1m[-2])
        else:
            price_change_1m = 0.0
        
        # 安全计算成交量比率（使用前5根K线的成交量，排除当前）
        historical_volumes = []
        if len(volumes_1m) >= 6:  # 确保有足够的历史数据
            historical_volumes = volumes_1m[-6:-1]  # 索引-6到-2，共5个元素
        
        vol_ratio = safe_calculate_vol_ratio(current_volume, historical_volumes)
        
        # 记录分析信号
        log_signal(
            symbol=symbol,
            price=current_price,
            price_change_1m=price_change_1m,
            vol_ratio=vol_ratio,
            coo_value=coo_value,
            trend_direction='up' if current_price > ma_5m else 'down',
            signal_strength='HIGH' if vol_ratio > 3.0 else 'MEDIUM',
            action='ANALYZED',
            market_condition='NORMAL'
        )
        
        # 2. 做多条件检查
        is_trend_up = current_price > ma_5m
        # 检查上影线是否过长
        bad_wick_up = False
        if highs_1m[-1] > 0:
            wick_up_ratio = (highs_1m[-1] - current_price) / highs_1m[-1]
            bad_wick_up = wick_up_ratio > 0.003
        
        if (is_trend_up and price_change_1m > 0 and 
            vol_ratio > VOL_MULTIPLIER and 
            coo_value < MAX_COO_ENTRY and 
            not bad_wick_up):
            
            # 检查是否为阳线（收盘价大于开盘价）
            if current_price > ohlcv_1m[-1][1]:
                # 资金费率检查
                rate_ok, funding_rate, funding_msg = check_funding_rate_simple(symbol, 'buy')
                
                # 记录过滤信号
                if not rate_ok:
                    log_signal(
                        symbol=symbol,
                        price=current_price,
                        price_change_1m=price_change_1m,
                        vol_ratio=vol_ratio,
                        coo_value=coo_value,
                        trend_direction='up',
                        funding_rate=funding_rate,
                        signal_strength='HIGH',
                        action='FILTERED',
                        filter_reason=funding_msg,
                        funding_ok=False
                    )
                    print(f"   ⚠️  {symbol} 做多被拒绝: {funding_msg}")
                    return
                
                # 执行开仓
                open_position(
                    symbol, current_price, 'buy', 
                    f"MA25之上+放量{vol_ratio:.1f}x", 
                    funding_msg,
                    vol_ratio=vol_ratio,
                    funding_rate=funding_rate,
                    price_change_1m=price_change_1m,
                    coo_value=coo_value
                )

        # 3. 做空条件检查
        is_trend_down = current_price < ma_5m
        # 检查下影线是否过长
        bad_wick_down = False
        if current_price > 0:
            wick_down_ratio = (current_price - lows_1m[-1]) / current_price
            bad_wick_down = wick_down_ratio > 0.003
        
        if (is_trend_down and price_change_1m < 0 and 
            vol_ratio > VOL_MULTIPLIER and 
            coo_value > MIN_COO_ENTRY and 
            not bad_wick_down):
            
            # 检查是否为阴线（收盘价小于开盘价）
            if current_price < ohlcv_1m[-1][1]:
                # 资金费率检查
                rate_ok, funding_rate, funding_msg = check_funding_rate_simple(symbol, 'sell')
                
                # 记录过滤信号
                if not rate_ok:
                    log_signal(
                        symbol=symbol,
                        price=current_price,
                        price_change_1m=price_change_1m,
                        vol_ratio=vol_ratio,
                        coo_value=coo_value,
                        trend_direction='down',
                        funding_rate=funding_rate,
                        signal_strength='HIGH',
                        action='FILTERED',
                        filter_reason=funding_msg,
                        funding_ok=False
                    )
                    print(f"   ⚠️  {symbol} 做空被拒绝: {funding_msg}")
                    return
                
                # 执行开仓
                open_position(
                    symbol, current_price, 'sell', 
                    f"MA25之下+放量{vol_ratio:.1f}x", 
                    funding_msg,
                    vol_ratio=vol_ratio,
                    funding_rate=funding_rate,
                    price_change_1m=price_change_1m,
                    coo_value=coo_value
                )

    except ZeroDivisionError as e:
        print(f"❌ {symbol} 分析出错: 除零错误 - {e}")
        log_signal(
            symbol=symbol,
            price=0,
            action='ERROR',
            filter_reason='除零错误',
            technical_ok=False
        )
    except Exception as e:
        print(f"❌ {symbol} 分析出错: {e}")
        log_signal(
            symbol=symbol,
            price=0,
            action='ERROR',
            filter_reason=str(e)[:50],
            technical_ok=False
        )

# ==================== 9. 持仓监控（放宽止损） ====================

def track_positions():
    """监控并管理持仓 - 放宽止损系统"""
    global trade_balance
    removes = []
    
    # 只在有持仓时显示标题
    if simulated_positions:
        print(f"\n{'='*20} [持仓监控] {'='*20}")
    
    for symbol, pos in list(simulated_positions.items()):
        try:
            # 如果已经触发完全止损，跳过
            if pos.get('full_stop_triggered', False):
                removes.append(symbol)
                continue
            
            current_price = 0
            roe = 0.0
            pnl_usdt = 0.0
            entry_price = pos['entry']
            
            # 获取当前价格
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    if current_price > 0:
                        break
                except:
                    if attempt < max_attempts - 1:
                        time.sleep(0.1)
                        continue
            
            # 如果交易所获取失败，尝试使用缓存
            if current_price <= 0 and symbol in price_history and price_history[symbol]:
                current_price = price_history[symbol][-1][1]
            
            if current_price <= 0:
                print(f"   ⚠️  无法获取{symbol}的有效价格，跳过")
                continue
            
            # 防零保护：确保entry_price不为零
            if entry_price == 0:
                print(f"   ⚠️  {symbol} 开仓价格为零，跳过")
                continue
            
            # 计算ROE和盈亏
            if pos['side'] == 'buy':
                roe = (current_price - entry_price) / entry_price * LEVERAGE
            else:  # sell
                roe = (entry_price - current_price) / entry_price * LEVERAGE
            
            pnl_usdt = roe * MARGIN_PER_TRADE
            
            # 更新最高ROE记录
            if roe > pos['highest_roe']:
                pos['highest_roe'] = roe
            if roe > pos['max_roe']: 
                pos['max_roe'] = roe
            
            # 计算剩余仓位比例
            remaining_ratio = 0.0
            if pos['amount'] > 0:
                remaining_ratio = 1.0 - (pos['closed_amount'] / pos['amount'])
            
            if remaining_ratio < 0.01:
                removes.append(symbol)
                continue
            
            action = None
            close_ratio = 0.0
            reason = ""
            is_graded_stop = False  # 🆕 标记是否为分级止损
            
            # ========== 🆕 放宽的分级硬止损系统 ==========
            if ENABLE_GRADED_STOP_LOSS:
                graded_stop_triggered = pos.get('graded_stop_triggered', {})
                
                for i, level in enumerate(GRADED_STOP_LEVELS):
                    level_key = f'level_{i}'
                    
                    # 检查是否达到该级别止损阈值且尚未触发
                    if roe <= level['roe_threshold'] and not graded_stop_triggered.get(level_key, False):
                        action = f"🛑 {level['name']}"
                        close_ratio = level['close_ratio'] * remaining_ratio
                        reason = f"分级止损: {level['name']}"
                        graded_stop_triggered[level_key] = True
                        pos['graded_stop_triggered'] = graded_stop_triggered
                        is_graded_stop = True
                        
                        # 如果是强制平仓级别，标记完全止损
                        if i == len(GRADED_STOP_LEVELS) - 1:
                            pos['full_stop_triggered'] = True
                        
                        break  # 只触发一个级别的止损
            
            # 如果没有触发分级止损，检查其他止损止盈条件
            if not is_graded_stop:
                # 1. 保本止损 (降低门槛)
                if pos['max_roe'] > BREAKEVEN_TRIGGER_ROE and roe < 0.01:
                    action = "🛡️ 保本"
                    close_ratio = remaining_ratio
                    reason = f"BE(最高{pos['max_roe']*100:.1f}%)"
                
                # 2. 正常止盈
                elif pos['tp_level'] == 0 and roe >= TP_ROE_1:
                    action = "💰 TP1"
                    close_ratio = PARTIAL_CLOSE_RATIO_1 * remaining_ratio
                    pos['tp_level'] = 1
                    pos['current_stop_loss'] = 0.01  # TP1后止损上移到+1% (降低)
                    reason = f"TP1({roe*100:.1f}%)"
                
                elif pos['tp_level'] == 1 and roe >= TP_ROE_2:
                    action = "💰 TP2"
                    close_ratio = PARTIAL_CLOSE_RATIO_2 * remaining_ratio
                    pos['tp_level'] = 2
                    pos['current_stop_loss'] = 0.06  # TP2后止损上移到+6% (降低)
                    reason = f"TP2({roe*100:.1f}%)"
                
                elif pos['tp_level'] == 2 and roe >= TP_ROE_3:
                    action = "🚀 TP3"
                    close_ratio = PARTIAL_CLOSE_RATIO_3 * remaining_ratio
                    pos['tp_level'] = 3
                    pos['current_stop_loss'] = 0.15  # TP3后止损上移到+15% (降低)
                    reason = f"TP3({roe*100:.1f}%)"
                
                # 3. 动态止损
                elif pos['tp_level'] > 0 and roe <= pos['current_stop_loss']:
                    action = "📉 动态止损"
                    close_ratio = remaining_ratio
                    reason = f"D-SL({pos['current_stop_loss']*100:.1f}%)"
                
                # 4. 回落止盈 (放宽条件)
                elif pos['tp_level'] >= 1 and pos['highest_roe'] > TP_ROE_1:
                    drawdown_points = (pos['highest_roe'] - roe) * 100
                    
                    if drawdown_points > 20:  # 从15提高到20
                        action = "📉 回落止盈"
                        if drawdown_points > 40:  # 从30提高到40
                            close_ratio = 0.5 * remaining_ratio
                        elif drawdown_points > 30:  # 从20提高到30
                            close_ratio = 0.3 * remaining_ratio
                        else:
                            close_ratio = 0.2 * remaining_ratio
                        reason = f"回落{drawdown_points:.1f}点"
            
            # 执行平仓
            if action:
                close_amount = pos['amount'] * close_ratio
                closed_pnl = pnl_usdt * close_ratio
                
                if LIVE_TRADING:
                    try:
                        close_side = 'sell' if pos['side'] == 'buy' else 'buy'
                        exchange.create_order(symbol, 'market', close_side, close_amount, params={'reduceOnly': True})
                        
                        # 分级止损通知
                        if is_graded_stop:
                            send_telegram(f"🛡️ **分级止损**\n`{symbol}` {action}\n收益: {closed_pnl:.2f}U | ROE: {roe*100:.1f}%\n当前仓位剩余: {remaining_ratio-close_ratio:.0f}%")
                        else:
                            send_telegram(f"🚨 **实盘平仓**\n`{symbol}` {action}\n收益: {closed_pnl:.2f}U | ROE: {roe*100:.1f}%\n原因: {reason}")
                    except Exception as e:
                        print(f"   └── ❌ 平仓失败: {e}")
                        continue
                else:
                    trade_balance += closed_pnl
                    
                    # Telegram消息
                    if is_graded_stop:
                        send_telegram(f"{action} `{symbol}`\n收益: {closed_pnl:.2f}U | ROE: {roe*100:.1f}%\n剩余仓位: {(remaining_ratio-close_ratio)*100:.0f}%\n入场价: {entry_price:.4f} | 现价: {current_price:.4f}")
                    elif "回落" in action:
                        send_telegram(f"{action} `{symbol}`\n收益: {closed_pnl:.2f}U | ROE: {roe*100:.1f}%\n最高: {pos['highest_roe']*100:.1f}% | 原因: {reason}")
                    else:
                        send_telegram(f"{action} `{symbol}`\n收益: {closed_pnl:.2f}U | ROE: {roe*100:.1f}%")
                
                print(f"   └── ✅ {action}: 平{close_ratio*100:.0f}%仓，收益{closed_pnl:.2f}U")
                pos['closed_amount'] += close_amount
                
                # 记录交易日志
                hold_time = int(time.time() - pos.get('log_open_time', time.time()))
                log_trade(
                    symbol=symbol,
                    side=pos['side'],
                    entry_price=pos.get('log_entry_price', entry_price),
                    exit_price=current_price,
                    quantity=close_amount,
                    roe_pct=roe * 100,
                    pnl_usdt=closed_pnl,
                    hold_time=hold_time,
                    open_reason=pos.get('log_open_reason', 'N/A'),
                    close_reason=f"{action}: {reason}",
                    funding_rate=pos.get('log_funding_rate', 0.0),
                    vol_ratio=pos.get('log_vol_ratio', 1.0),
                    max_roe=pos['highest_roe'] * 100,
                    tp_level=pos['tp_level']
                )
                
                # 检查是否完全平仓
                if pos['closed_amount'] >= pos['amount'] * 0.99:
                    removes.append(symbol)
            
            # 显示持仓信息（包含分级止损状态）
            tp_status = f"TP{pos['tp_level']}" if pos['tp_level'] > 0 else "未触发"
            
            # 显示已触发的分级止损
            graded_status = ""
            if ENABLE_GRADED_STOP_LOSS:
                graded_stop_triggered = pos.get('graded_stop_triggered', {})
                triggered_levels = []
                for i, level in enumerate(GRADED_STOP_LEVELS):
                    if graded_stop_triggered.get(f'level_{i}', False):
                        triggered_levels.append(level['name'])
                
                if triggered_levels:
                    graded_status = f" | 已止损: {','.join(triggered_levels)}"
            
            # 预警：接近下一个分级止损线时显示
            warning = ""
            if ENABLE_GRADED_STOP_LOSS:
                graded_stop_triggered = pos.get('graded_stop_triggered', {})
                
                # 找到下一个未触发的止损级别
                next_level = None
                for i, level in enumerate(GRADED_STOP_LEVELS):
                    level_key = f'level_{i}'
                    if not graded_stop_triggered.get(level_key, False):
                        next_level = level
                        break
                
                if next_level and roe <= next_level['roe_threshold'] * 1.1:  # 从1.2降低到1.1
                    warning = f" ⚠️接近{next_level['name']}"
            
            # 每60秒显示一次持仓状态
            hold_time = int(time.time() - pos['open_time'])
            if hold_time % 60 == 0 or action:
                print(f"💎 {symbol:<12} {pos['side']} | ROE: {roe*100:>6.1f}%{warning}{graded_status} | 状态: {tp_status:<6} | 最高: {pos['highest_roe']*100:>5.1f}% | 持仓: {hold_time:>4}s")
            
            save_data()
            
        except ZeroDivisionError as e:
            print(f"❌ 跟踪 {symbol} 出错: 除零错误 - {e}")
        except Exception as e:
            print(f"❌ 跟踪 {symbol} 出错: {e}")
            traceback.print_exc()
    
    if simulated_positions:
        print(f"{'='*50}")
    
    # 移除已完全平仓的仓位
    for symbol in removes:
        if symbol in simulated_positions:
            del simulated_positions[symbol]
            print(f"   🗑️  已移除 {symbol} 的持仓记录")

# ==================== 10. 紧急止损监控线程 ====================
def emergency_stop_loss_monitor():
    """紧急止损监控线程 - 确保分级止损及时触发"""
    while running_flag:
        try:
            for symbol, pos in list(simulated_positions.items()):
                # 跳过已触发完全止损的仓位
                if pos.get('full_stop_triggered', False):
                    continue
                    
                try:
                    # 快速获取价格
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    
                    if current_price <= 0:
                        continue
                    
                    entry_price = pos['entry']
                    if entry_price <= 0:
                        continue
                    
                    # 计算ROE
                    if pos['side'] == 'buy':
                        roe = (current_price - entry_price) / entry_price * LEVERAGE
                    else:
                        roe = (entry_price - current_price) / entry_price * LEVERAGE
                    
                    # 检查分级止损
                    if ENABLE_GRADED_STOP_LOSS:
                        graded_stop_triggered = pos.get('graded_stop_triggered', {})
                        
                        for i, level in enumerate(GRADED_STOP_LEVELS):
                            level_key = f'level_{i}'
                            
                            # 检查是否达到该级别止损阈值且尚未触发
                            if roe <= level['roe_threshold'] and not graded_stop_triggered.get(level_key, False):
                                remaining_ratio = 1.0 - (pos['closed_amount'] / pos['amount']) if pos['amount'] > 0 else 0
                                
                                if remaining_ratio > 0.01:
                                    print(f"🚨 紧急分级止损监控: {symbol} ROE={roe*100:.1f}%，触发{level['name']}")
                                    
                                    # 执行分级止损
                                    close_ratio = level['close_ratio'] * remaining_ratio
                                    close_amount = pos['amount'] * close_ratio
                                    close_side = 'sell' if pos['side'] == 'buy' else 'buy'
                                    
                                    if LIVE_TRADING:
                                        try:
                                            exchange.create_order(
                                                symbol, 'market', close_side, close_amount, 
                                                params={'reduceOnly': True}
                                            )
                                            print(f"   ✅ 紧急分级止损订单已发送")
                                        except Exception as e:
                                            print(f"   ❌ 紧急分级止损失败: {e}")
                                    
                                    # 更新模拟余额
                                    if not LIVE_TRADING:
                                        global trade_balance
                                        closed_pnl = roe * MARGIN_PER_TRADE * close_ratio
                                        trade_balance += closed_pnl
                                    
                                    # 更新仓位记录
                                    pos['closed_amount'] += close_amount
                                    graded_stop_triggered[level_key] = True
                                    pos['graded_stop_triggered'] = graded_stop_triggered
                                    
                                    # 如果是强制平仓级别，标记完全止损
                                    if i == len(GRADED_STOP_LEVELS) - 1:
                                        pos['full_stop_triggered'] = True
                                    
                                    # 记录交易日志
                                    hold_time = int(time.time() - pos.get('log_open_time', time.time()))
                                    log_trade(
                                        symbol=symbol,
                                        side=pos['side'],
                                        entry_price=pos.get('log_entry_price', entry_price),
                                        exit_price=current_price,
                                        quantity=close_amount,
                                        roe_pct=roe * 100,
                                        pnl_usdt=roe * MARGIN_PER_TRADE * close_ratio,
                                        hold_time=hold_time,
                                        open_reason=pos.get('log_open_reason', 'N/A'),
                                        close_reason=f"紧急{level['name']}",
                                        funding_rate=pos.get('log_funding_rate', 0.0),
                                        vol_ratio=pos.get('log_vol_ratio', 1.0),
                                        max_roe=pos['highest_roe'] * 100,
                                        tp_level=pos['tp_level']
                                    )
                                    
                                    # 发送紧急通知
                                    pnl = roe * MARGIN_PER_TRADE * close_ratio
                                    send_telegram(f"🚨 **紧急分级止损** `{symbol}`\n{level['name']}\n收益: {pnl:.2f}U | ROE: {roe*100:.1f}%\n剩余仓位: {(remaining_ratio-close_ratio)*100:.0f}%")
                
                except Exception as e:
                    continue
            
            time.sleep(1)  # 紧急监控每秒检查一次
            
        except Exception:
            time.sleep(2)

# ==================== 11. 主程序入口 ====================
def main():
    global running_flag, trade_balance, ENABLE_FUNDING_FILTER, ENABLE_LOGGING
    
    print(f"🔥 V15.7 (放宽止损版) 启动...")
    print(f"📊 策略: 放宽三级硬止损系统 + 实时价格监控")
    print(f"📁 工作目录: {BASE_DIR}")
    
    # 显示分级止损配置
    print(f"🛡️  分级硬止损配置:")
    for level in GRADED_STOP_LEVELS:
        print(f"   {level['name']}: ROE ≤ {level['roe_threshold']*100:.0f}% → 减仓{level['close_ratio']*100:.0f}%")
    
    # 初始化日志系统
    if ENABLE_LOGGING:
        if init_log_system():
            print("✅ CSV日志系统已初始化")
            print(f"📁 日志目录: {LOG_DIR}")
            print(f"📁 数据目录: {DATA_DIR}")
        else:
            print("⚠️  CSV日志系统初始化失败，继续运行")
    
    # API检查
    if "API_KEY" in API_KEY and LIVE_TRADING: 
        print("❌ 实盘模式必须配置正确的 API KEY")
        sys.exit(1)
    
    # 启动后台线程
    threading.Thread(target=background_price_snapshot, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=emergency_stop_loss_monitor, daemon=True).start()
    
    print("⏳ 数据预热 (10秒)...")
    time.sleep(10)
    
    # 加载历史数据
    load_data()
    
    # 获取初始余额
    if LIVE_TRADING:
        try:
            balance_info = exchange.fetch_balance()
            trade_balance = balance_info['free']['USDT']
            print(f"✅ 实盘启动 | 余额: {trade_balance:.2f} U")
        except Exception as e: 
            print(f"❌ 获取余额失败: {e}")
            sys.exit(1)
    else:
        print(f"⚡ 模拟启动 | 初始余额: {trade_balance:.2f} U")
    
    # 显示策略配置
    print(f"\n📊 策略配置:")
    print(f"   杠杆: {LEVERAGE}x (降低)")
    print(f"   单笔保证金: {MARGIN_PER_TRADE:.1f}U (降低)")
    print(f"   分级硬止损: {'🟢 开启' if ENABLE_GRADED_STOP_LOSS else '🔴 关闭'}")
    print(f"   资金费率过滤: {'🟢 开启' if ENABLE_FUNDING_FILTER else '🔴 关闭'}")
    print(f"   CSV日志记录: {'🟢 开启' if ENABLE_LOGGING else '🔴 关闭'}")
    print(f"   TP1: {TP_ROE_1*100:.1f}% | TP2: {TP_ROE_2*100:.1f}% | TP3: {TP_ROE_3*100:.1f}%")
    print(f"   紧急止损监控: 🟢 已启用")
    print(f"   日志路径: {LOG_DIR}")
    print("-" * 50)
    
    cycle = 0
    try:
        while running_flag:
            cycle += 1
            
            # 持仓监控（主监控循环）
            if simulated_positions: 
                track_positions()
            
            # 扫描快速变动币种
            movers = get_fast_movers()
            
            # 显示扫描结果
            if movers:
                top_symbols = []
                for symbol, change in movers[:3]:
                    symbol_name = symbol.split(':')[0] if ':' in symbol else symbol
                    direction = '↑' if change > 0 else '↓'
                    top_symbols.append(f"{symbol_name}{direction}")
                
                filter_status = "🟢" if ENABLE_FUNDING_FILTER else "🔴"
                log_status = "📝" if ENABLE_LOGGING else " "
                graded_stop_status = "🛡️" if ENABLE_GRADED_STOP_LOSS else " "
                print(f"\r🔍 扫描: {top_symbols} | 持仓: {len(simulated_positions)} | 余额: {trade_balance:.1f}U {log_status}{graded_stop_status}", end="", flush=True)
            else:
                print(f"\r⏳ 扫描中... | 持仓: {len(simulated_positions)} | 余额: {trade_balance:.1f}U      ", end="", flush=True)
            
            # 分析前5个快速变动币种
            for symbol, change in movers[:5]:
                if symbol not in simulated_positions: 
                    analyze_and_trade(symbol)
            
            # 等待下一次扫描
            time.sleep(SCAN_INTERVAL)
            
    except KeyboardInterrupt:
        running_flag = False
        print("\n\n🛑 用户中断，正在退出...")
    except Exception as e:
        running_flag = False
        print(f"\n❌ 程序异常: {e}")
        traceback.print_exc()
    finally:
        # 保存数据并退出
        save_data()
        print("✅ 数据已保存")
        print(f"📁 日志文件保存在: {LOG_DIR}")
        print(f"📁 数据文件保存在: {DATA_FILE}")

# ==================== 初始化交易所连接 ====================
try:
    exchange = ccxt.binanceusdm({
        'apiKey': API_KEY,  # ✅ 使用环境变量
        'secret': SECRET_KEY,  # ✅ 使用环境变量
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
        # 🔧 移除代理（云服务器不需要）
        'timeout': 10000
    })
    exchange.load_markets()
    print("✅ 交易所连接成功")
except Exception as e:
    print(f"❌ 连接交易所失败: {e}")
    sys.exit(1)

