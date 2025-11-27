import base64
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import traceback
import hmac
import hashlib
import json
import os
import threading
import gradio as gr
from dotenv import load_dotenv

# ==============================================================================
# ========== CẤU HÌNH TRUNG TÂM ==========
# ==============================================================================

# --- Cài đặt chung ---
VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
CHART_TYPE = "5m"
LEVERAGE = 30
ORDER_TIMEOUT_MINUTES = 36

### CỜ BẬT/TẮT CHIẾN LƯỢC ###
ALLOW_SHORT_TRADES = True
ALLOW_LONG_TRADES = True

# --- Cấu hình chiến lược SHORT ---
SHORT_WICK_THRESHOLD = 1.5           # Tỉ lệ Râu trên / Thân nến
SHORT_BODY_SIZE_THRESHOLD = 0.03/100   # Thân nến phải lớn hơn 0.03% so với giá mở cửa
SHORT_SMALL_WICK_THRESHOLD = 0.02/100  # Râu dưới phải nhỏ hơn 0.02% so với giá thấp nhất
SHORT_SIGNAL_WICK_MIN_PERCENT = 0.25/100 # Râu trên phải lớn hơn 0.2% so với giá cao nhất

# --- Cấu hình chiến lược LONG ---
LONG_LOWER_WICK_THRESHOLD = 1.5          # Tỉ lệ Râu dưới / Thân nến
LONG_BODY_SIZE_THRESHOLD = 0.03/100    # Thân nến phải lớn hơn 0.03%
LONG_SMALL_WICK_THRESHOLD = 0.02/100   # Râu trên phải nhỏ hơn 0.02%
LONG_SIGNAL_WICK_MIN_PERCENT = 0.25/100  # Râu dưới phải lớn hơn 0.25% (Theo yêu cầu)


# --- Lấy cấu hình API OKX ---
load_dotenv()
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE_URL = "https://www.okx.com"

# --- Cấu hình Slack ---
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#trading-alerts")

# --- Biến toàn cục ---
pending_orders = []
ORDERS_LOCK = threading.Lock()

# --- Kiểm tra cấu hình API ---
if not all([OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE]):
    print("❌ Lỗi: Vui lòng thiết lập đầy đủ OKX_API_KEY, OKX_SECRET_KEY, và OKX_PASSPHRASE trong file .env")
    exit(1)

# --- CẤU HÌNH GIAO DỊCH CHO TỪNG SYMBOL ---
SYMBOLS = [
    {
        "symbol": "BTC-USDT-SWAP",
        "position_size_usdt": 6,
        "volume_multiplier": 1.0, # (Không còn được sử dụng)
        "rr_ratio": 1,
        "lot_size": 0.001
    }
]

# ==============================================================================
# ========== CÁC HÀM TIỆN ÍCH (SLACK & OKX API) ==========
# ==============================================================================

def send_slack_alert(message, is_critical=False):
    # Gửi cảnh báo đến Slack
    if not SLACK_WEBHOOK_URL: return
    try:
        prefix = "🚨 *CẢNH BÁO NGHIÊM TRỌNG* 🚨\n" if is_critical else "⚠️ *CẢNH BÁO* ⚠️\n"
        payload = {"text": prefix + message, "channel": SLACK_CHANNEL, "username": "Trading Bot", "icon_emoji": ":robot_face:"}
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        print("✅ Đã gửi cảnh báo đến Slack")
    except Exception as e:
        print(f"⚠️ Lỗi gửi Slack: {e}")

def okx_signature(timestamp, method, request_path, body=""):
    # Tạo chữ ký OKX
    message = timestamp + method + request_path + body
    mac = hmac.new(bytes(OKX_SECRET_KEY, 'utf-8'), bytes(message, 'utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_request(method, endpoint, params=None, body=None):
    # Thực hiện yêu cầu API đến OKX
    try:
        timestamp = datetime.utcnow().isoformat("T", "milliseconds") + "Z"
        request_path = endpoint
        if method == "GET" and params:
            request_path += "?" + "&".join([f"{k}={v}" for k, v in params.items()])
        body_str = json.dumps(body) if body else ""
        sign = okx_signature(timestamp, method, request_path, body_str)
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY, 'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp, 'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json'
        }
        url = OKX_BASE_URL + request_path
        response = requests.request(method, url, headers=headers, data=body_str, timeout=10)
        return response.json()
    except Exception as e:
        print(f"❌ Lỗi OKX API Request: {e}")
        return None

def set_leverage(symbol, leverage, posSide):
    # Thiết lập đòn bẩy
    endpoint = "/api/v5/account/set-leverage"
    body = {"instId": symbol, "lever": str(leverage), "mgnMode": "isolated", "posSide": posSide}
    return okx_request("POST", endpoint, body=body)

def place_order(symbol, side, posSide, price, sl_price, tp_price, size):
    # Đặt lệnh limit có SL/TP
    leverage_result = set_leverage(symbol, LEVERAGE, posSide)
    if not leverage_result or leverage_result.get('code') != '0':
        print(f"❌ Lỗi thiết lập đòn bẩy cho {posSide}: {leverage_result}")
        return None
        
    endpoint = "/api/v5/trade/order"
    body = {
        "instId": symbol, "tdMode": "isolated", "side": side, "posSide": posSide,
        "ordType": "limit", "px": str(price), "sz": str(size),
        "slTriggerPx": str(sl_price), "slOrdPx": "-1",
        "tpTriggerPx": str(tp_price), "tpOrdPx": "-1"
    }
    return okx_request("POST", endpoint, body=body)

def get_order_status(symbol, order_id):
    # Lấy trạng thái lệnh
    endpoint = "/api/v5/trade/order"
    params = {"instId": symbol, "ordId": order_id}
    return okx_request("GET", endpoint, params=params)

def cancel_order(symbol, order_id):
    # Hủy lệnh
    endpoint = "/api/v5/trade/cancel-order"
    body = {"instId": symbol, "ordId": order_id}
    return okx_request("POST", endpoint, body=body)

def get_account_balance():
    # Lấy số dư tài khoản
    endpoint = "/api/v5/account/balance"
    params = {"ccy": "USDT"}
    result = okx_request("GET", endpoint, params=params)
    if result and result.get('code') == '0' and result['data']:
        for detail in result['data'][0]['details']:
            if detail['ccy'] == 'USDT':
                return float(detail['availBal'])
    return 0

# ========== CÁC HÀM MỚI ĐỂ QUẢN LÝ VỊ THẾ (DỜI SL) ==========
def get_open_positions():
    """Lấy tất cả các vị thế đang mở."""
    endpoint = "/api/v5/account/positions"
    params = {"instType": "SWAP"} 
    result = okx_request("GET", endpoint, params=params)
    if result and result.get('code') == '0' and result['data']:
        open_positions = [pos for pos in result['data'] if float(pos.get('pos', '0')) != 0]
        return open_positions
    return []

def get_market_ticker(symbol):
    """Lấy giá thị trường (ticker) hiện tại cho một symbol."""
    endpoint = "/api/v5/market/ticker"
    params = {"instId": symbol}
    result = okx_request("GET", endpoint, params=params)
    if result and result.get('code') == '0' and result['data']:
        return float(result['data'][0]['last'])
    return None

def get_pending_algo_orders(symbol, pos_side, order_type="sl"):
    """Lấy các lệnh algo (SL/TP) đang chờ."""
    endpoint = "/api/v5/trade/orders-algo-pending"
    params = {
        "instType": "SWAP",
        "instId": symbol,
        "ordType": order_type
    }
    result = okx_request("GET", endpoint, params=params)
    if result and result.get('code') == '0' and result['data']:
        matching_orders = [
            order for order in result['data'] 
            if order.get('posSide') == pos_side and order.get('state') == 'live'
        ]
        return matching_orders
    return []

def modify_algo_order_sl(symbol, algo_id, new_sl_price):
    """Sửa đổi giá SL của một lệnh algo đang chạy."""
    endpoint = "/api/v5/trade/amend-algo-order"
    body = {
        "instId": symbol,
        "algoId": algo_id,
        "newSlTriggerPx": str(new_sl_price),
        "newSlOrdPx": "-1"
    }
    print(f"   -> Gửi yêu cầu dời SL cho AlgoID {algo_id} về {new_sl_price}")
    return okx_request("POST", endpoint, body=body)

# ==============================================================================
# ========== LOGIC GIAO DỊCH CỐT LÕI ==========
# ==============================================================================

def fetch_signal_candle(symbol):
    """
    Lấy 2 nến: data[0] (tín hiệu) và data[1] (nến trước) để so sánh volume.
    """
    try:
        url = f"{OKX_BASE_URL}/api/v5/market/history-candles"
        # Lấy 2 nến: data[0] (tín hiệu) và data[1] (nến trước)
        params = {"instId": symbol, "bar": CHART_TYPE, "limit": "2"} 
        
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        # Kiểm tra cần 2 nến
        if data.get('code') != '0' or not data.get('data') or len(data['data']) < 2:
            print(f"❌ Không đủ dữ liệu nến cho {symbol} (cần 2 nến): {data}")
            return None, None # Trả về 2 None
        
        def parse_candle(candle_data):
            """
            Hàm này parse mảng dữ liệu nến.
            candle_data[1] = Open, candle_data[2] = High,
            candle_data[3] = Low, candle_data[4] = Close, candle_data[5] = Volume
            """
            return {
                "open": float(candle_data[1]), "high": float(candle_data[2]),
                "low": float(candle_data[3]), "close": float(candle_data[4]),
                "volume": float(candle_data[5]) # Volume là phần tử thứ 5 (index 5)
            }
        
        signal_candle = parse_candle(data['data'][0])
        prev_candle = parse_candle(data['data'][1])

        # LOG DEBUG (Đã cập nhật để hiển thị volume)
        print("   --- LOG DEBUG API (RAW) ---")
        print(f"   [data[0]] O:{data['data'][0][1]} H:{data['data'][0][2]} L:{data['data'][0][3]} C:{data['data'][0][4]} V:{data['data'][0][5]} (TÍN HIỆU)")
        print(f"   [data[1]] O:{data['data'][1][1]} H:{data['data'][1][2]} L:{data['data'][1][3]} C:{data['data'][1][4]} V:{data['data'][1][5]} (NẾN TRƯỚC)")
        print("   -----------------------------")
        
        return signal_candle, prev_candle # Trả về cả hai nến
    except Exception as e:
        print(f"❌ Lỗi lấy nến {symbol}: {e}")
        return None, None

def analyze_short_signal(candle):
    # Phân tích tín hiệu SHORT
    try:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        if c >= o: return False 

        body_size = o - c
        upper_wick = h - o
        lower_wick = c - l

        body_size_percent = body_size / o if o > 0 else 0
        upper_wick_percent = upper_wick / h if h > 0 else 0
        lower_wick_percent = lower_wick / l if l > 0 else 0
        wick_to_body_ratio = upper_wick / body_size if body_size > 0 else 0
        
        print(f"   [CHECK SHORT]\n"
              f"   - % Body (so với giá mở cửa):   {body_size_percent:.4%}\n"
              f"   - % Râu trên (so với giá cao nhất): {upper_wick_percent:.4%}\n"
              f"   - % Râu dưới (so với giá thấp nhất):  {lower_wick_percent:.4%}\n"
              f"   - Tỉ lệ Râu trên/Thân:           {wick_to_body_ratio:.2f}")

        cond_body_size = body_size_percent >= SHORT_BODY_SIZE_THRESHOLD
        cond_wick_ratio = wick_to_body_ratio > SHORT_WICK_THRESHOLD
        cond_small_lower_wick = lower_wick_percent <= SHORT_SMALL_WICK_THRESHOLD
        cond_upper_wick_min_percent = upper_wick_percent >= SHORT_SIGNAL_WICK_MIN_PERCENT

        if cond_body_size and cond_wick_ratio and cond_small_lower_wick and cond_upper_wick_min_percent:
            return True
        return False
    except Exception as e:
        print(f"Lỗi phân tích nến SHORT: {e}")
        traceback.print_exc()
        return False

def analyze_long_signal(candle):
    # Phân tích tín hiệu LONG
    try:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        if c <= o: return False 

        body_size = c - o
        lower_wick = o - l
        upper_wick = h - c

        body_size_percent = body_size / o if o > 0 else 0
        lower_wick_percent = lower_wick / l if l > 0 else 0
        upper_wick_percent = upper_wick / h if h > 0 else 0
        wick_to_body_ratio = lower_wick / body_size if body_size > 0 else 0

        print(f"   [CHECK LONG]\n"
              f"   - % Body (so với giá mở cửa):    {body_size_percent:.4%}\n"
              f"   - % Râu trên (so với giá cao nhất):  {upper_wick_percent:.4%}\n"
              f"   - % Râu dưới (soVỚI giá thấp nhất):   {lower_wick_percent:.4%}\n"
              f"   - Tỉ lệ Râu dưới/Thân:            {wick_to_body_ratio:.2f}")

        cond_body_size = body_size_percent >= LONG_BODY_SIZE_THRESHOLD
        cond_wick_ratio = wick_to_body_ratio > LONG_LOWER_WICK_THRESHOLD
        cond_small_upper_wick = upper_wick_percent <= LONG_SMALL_WICK_THRESHOLD
        cond_lower_wick_min_percent = lower_wick_percent >= LONG_SIGNAL_WICK_MIN_PERCENT

        if cond_body_size and cond_wick_ratio and cond_small_upper_wick and cond_lower_wick_min_percent:
            return True
        return False
    except Exception as e:
        print(f"Lỗi phân tích nến LONG: {e}")
        traceback.print_exc()
        return False

def calculate_position_size(position_size_usdt, entry_price, lot_size, leverage):
    """
    Tính toán kích thước lệnh và làm tròn để đảm bảo là bội số của lot_size.
    (Đã fix lỗi 51121)
    """
    if entry_price <= 0 or lot_size <= 0:
        return 0
        
    raw_size = (position_size_usdt * leverage) / entry_price
    
    # 1. Tính số lượng lô (phải là số nguyên)
    # Lấy phần nguyên của (raw_size / lot_size)
    number_of_lots = int(raw_size / lot_size)
    
    # 2. Tính kích thước đã làm tròn (bội số của lot_size)
    adjusted_size = number_of_lots * lot_size
    
    # Làm tròn để tránh sai số float, đảm bảo là bội số của lot_size (0.001)
    adjusted_size = round(adjusted_size, 8) 
    
    print(f"   [DEBUG SIZE] Raw Size: {raw_size:.8f} | Lots: {number_of_lots} | Adjusted Size: {adjusted_size:.8f}")

    return adjusted_size

def execute_trade(sym_config, signal_candle, next_candle_open, signal_type):
    # Thực hiện giao dịch
    try:
        balance = get_account_balance()
        position_size_usdt = sym_config['position_size_usdt']

        if balance < position_size_usdt:
            print(f"❌ Số dư không đủ: {balance:.2f} USDT (cần {position_size_usdt} USDT)")
            send_slack_alert(f"💸 Số dư không đủ cho *{sym_config['symbol']}*. Cần {position_size_usdt} USDT nhưng chỉ có {balance:.2f} USDT.")
            return
        
        entry_price = next_candle_open
        
        if signal_type == "SHORT":
            side = "sell"
            posSide = "short"
            stop_loss = signal_candle['high'] + (signal_candle['high'] * 0.001)
            risk = stop_loss - entry_price
            if risk <= 0:
                print(f"❌ Lỗi: Risk (SHORT) không hợp lệ (<= 0). SL: {stop_loss}, Entry: {entry_price}")
                return
            tp_price = entry_price - (risk * sym_config['rr_ratio'])
            alert_icon = "📉"
            
        elif signal_type == "LONG":
            side = "buy"
            posSide = "long"
            stop_loss = signal_candle['low'] - (signal_candle['low'] * 0.001)
            risk = entry_price - stop_loss
            if risk <= 0:
                print(f"❌ Lỗi: Risk (LONG) không hợp lệ (<= 0). Entry: {entry_price}, SL: {stop_loss}")
                return
            tp_price = entry_price + (risk * sym_config['rr_ratio'])
            alert_icon = "🚀"
            
        else:
            print(f"❌ Lỗi: Không rõ signal_type: {signal_type}")
            return

        # Gọi hàm tính toán size đã được sửa lỗi
        position_size = calculate_position_size(
            position_size_usdt, 
            entry_price, 
            sym_config['lot_size'],
            LEVERAGE
        )
        
        if position_size <= 0:
            print(f"❌ Lỗi: Kích thước lệnh quá nhỏ sau khi làm tròn. Cân nhắc tăng 'position_size_usdt'.")
            return

        print(f"🎯 Chuẩn bị đặt lệnh {signal_type} {sym_config['symbol']} | Size: {position_size}")
        order_result = place_order(sym_config['symbol'], side, posSide, entry_price, stop_loss, tp_price, position_size)
        
        if order_result and order_result.get('code') == '0':
            order_id = order_result['data'][0]['ordId']
            print(f"✅ Đặt lệnh thành công! ID: {order_id}")
            with ORDERS_LOCK:
                pending_orders.append({
                    'orderId': order_id,
                    'symbol': sym_config['symbol'],
                    'place_time': datetime.now(ZoneInfo("UTC"))
                })
            send_slack_alert(f"{alert_icon} Đã đặt lệnh {signal_type} cho *{sym_config['symbol']}*:\n- Entry: `{entry_price}`\n- SL: `{stop_loss}`\n- TP: `{tp_price}`\n- Size: `{position_size}`\n- ID: `{order_id}`")
        else:
            print(f"❌ Lỗi đặt lệnh: {order_result}")
            # Báo cáo lỗi đặt lệnh lên Slack
            send_slack_alert(f"🔥 Lỗi khi đặt lệnh {signal_type} cho *{sym_config['symbol']}*:\n`{order_result}`", is_critical=True)

    except Exception as e:
        print(f"❌ Lỗi nghiêm trọng trong execute_trade: {e}")
        traceback.print_exc()

def check_and_cancel_stale_orders():
    # Kiểm tra và hủy lệnh quá hạn
    global pending_orders
    print(f"\n🔄 Bắt đầu kiểm tra {len(pending_orders)} lệnh đang chờ...")
    with ORDERS_LOCK:
        if not pending_orders: return
        orders_to_remove = []
        for order in pending_orders:
            if (datetime.now(ZoneInfo("UTC")) - order['place_time']).total_seconds() > ORDER_TIMEOUT_MINUTES * 60:
                print(f"   - Lệnh {order['orderId']} ({order['symbol']}) đã quá hạn...")
                status_result = get_order_status(order['symbol'], order['orderId'])
                if status_result and status_result.get('code') == '0':
                    state = status_result['data'][0].get('state')
                    if state == 'live':
                        print("     -> Đang hủy lệnh...")
                        cancel_result = cancel_order(order['symbol'], order['orderId'])
                        if cancel_result and cancel_result.get('code') == '0':
                            print(f"     ✅ Đã hủy lệnh {order['orderId']} thành công.")
                            send_slack_alert(f"🚫 Đã tự động hủy lệnh cho *{order['symbol']}* (ID: `{order['orderId']}`) vì quá hạn.")
                            orders_to_remove.append(order)
                    else:
                        print(f"     -> Trạng thái: {state.upper()}. Xóa khỏi danh sách.")
                        orders_to_remove.append(order)
        if orders_to_remove:
            pending_orders = [o for o in pending_orders if o not in orders_to_remove]

def manage_position_sl_to_entry():
    # Quản lý dời SL về điểm hòa vốn (entry)
    print(f"\n🔄 Bắt đầu kiểm tra dời SL cho các vị thế đang mở...")
    try:
        open_positions = get_open_positions()
        if not open_positions:
            print("   - Không có vị thế nào đang mở.")
            return

        for pos in open_positions:
            symbol = pos['instId']
            pos_side = pos['posSide'] # 'long' or 'short'
            entry_price = float(pos['avgPx']) 
            
            print(f"   - Đang kiểm tra vị thế {symbol} ({pos_side.upper()}) | Entry: {entry_price}")

            current_price = get_market_ticker(symbol)
            if not current_price:
                print(f"     -> Lỗi: Không lấy được giá ticker cho {symbol}")
                continue

            sl_orders = get_pending_algo_orders(symbol, pos_side, order_type="sl")
            if not sl_orders:
                print(f"     -> Không tìm thấy lệnh SL (algo) đang 'live' cho vị thế này.")
                continue
            
            sl_order = sl_orders[0] 
            original_sl_price = float(sl_order['slTriggerPx'])
            sl_algo_id = sl_order['algoId']

            if original_sl_price == entry_price:
                print(f"     -> SL đã ở điểm entry. Bỏ qua.")
                continue

            risk_amount = 0
            profit_target_1_1 = 0
            
            if pos_side == 'long':
                risk_amount = entry_price - original_sl_price
                if risk_amount <= 0: continue 
                profit_target_1_1 = entry_price + risk_amount
                
                if current_price >= profit_target_1_1:
                    print(f"     ✅ LONG ĐẠT 1:1 (Giá: {current_price} >= {profit_target_1_1}). Dời SL về {entry_price}")
                    result = modify_algo_order_sl(symbol, sl_algo_id, entry_price)
                    if result and result.get('code') == '0':
                        send_slack_alert(f"✅ Đã dời SL về Entry cho *{symbol} (LONG)*.\n- Entry: `{entry_price}`")
                    else:
                        print(f"     ❌ Lỗi dời SL: {result}")
                        send_slack_alert(f"🔥 Lỗi khi dời SL cho *{symbol} (LONG)*:\n`{result}`", is_critical=True)
                else:
                    print(f"     -> LONG chưa đạt 1:1 (Giá: {current_price} < {profit_target_1_1})")

            elif pos_side == 'short':
                risk_amount = original_sl_price - entry_price
                if risk_amount <= 0: continue
                profit_target_1_1 = entry_price - risk_amount
                
                if current_price <= profit_target_1_1:
                    print(f"     ✅ SHORT ĐẠT 1:1 (Giá: {current_price} <= {profit_target_1_1}). Dời SL về {entry_price}")
                    result = modify_algo_order_sl(symbol, sl_algo_id, entry_price)
                    if result and result.get('code') == '0':
                        send_slack_alert(f"✅ Đã dời SL về Entry cho *{symbol} (SHORT)*.\n- Entry: `{entry_price}`")
                    else:
                        print(f"     ❌ Lỗi dời SL: {result}")
                        send_slack_alert(f"🔥 Lỗi khi dời SL cho *{symbol} (SHORT)*:\n`{result}`", is_critical=True)
                else:
                    print(f"     -> SHORT chưa đạt 1:1 (Giá: {current_price} > {profit_target_1_1})")
                    
    except Exception as e:
        print(f"❌ Lỗi nghiêm trọng trong lúc quản lý dời SL: {e}")
        traceback.print_exc()
        send_slack_alert(f"🔥 Lỗi nghiêm trọng khi chạy `manage_position_sl_to_entry`:\n`{traceback.format_exc()}`", is_critical=True)


# ==============================================================================
# ========== TÁC VỤ CHÍNH VÀ LẬP LỊCH ==========
# ==============================================================================

def trading_bot_task():
    """Hàm chính thực hiện toàn bộ logic quét và giao dịch."""
    print(f"\n{'='*50}\n🕒 Bắt đầu chu kỳ quét lúc: {datetime.now(VIETNAM_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}\n{'='*50}")
    for sym_config in SYMBOLS:
        symbol = sym_config['symbol']
        print(f"🔍 Đang phân tích {symbol}...")
        
        # Nhận 2 nến: tín hiệu (data[0]) và nến trước (data[1])
        signal_candle, prev_candle = fetch_signal_candle(symbol)
        
        # Kiểm tra tính hợp lệ của dữ liệu nến
        if not signal_candle or not prev_candle:
            continue
            
        print(f"   --- Thông tin nến tín hiệu (data[0]) ---")
        print(f"   - Mở cửa (Open):   {signal_candle['open']}")
        print(f"   - Cao nhất (High):  {signal_candle['high']}")
        print(f"   - Thấp nhất (Low):   {signal_candle['low']}")
        print(f"   - Đóng cửa (Close): {signal_candle['close']}")
        print(f"   - Volume: {signal_candle['volume']:.2f} | Volume nến trước: {prev_candle['volume']:.2f}")
        print(f"   -------------------------------")

        # ĐIỀU KIỆN MỚI: Volume nến tín hiệu (data[0]) phải lớn hơn nến trước (data[1])
        is_high_volume = signal_candle['volume'] > prev_candle['volume']
        
        if not is_high_volume:
            print("   ⚠️ Bỏ qua: Volume nến tín hiệu (data[0]) KHÔNG lớn hơn volume nến trước (data[1]).")
            continue
            
        print("   ✅ Đã đạt điều kiện Volume (Volume hiện tại > Volume trước đó). Tiếp tục kiểm tra tín hiệu...")


        is_signal_short = False
        is_signal_long = False

        if ALLOW_SHORT_TRADES:
            is_signal_short = analyze_short_signal(signal_candle)
            
        if ALLOW_LONG_TRADES:
            is_signal_long = analyze_long_signal(signal_candle)

        if is_signal_short:
            print(f"   ⚡ PHÁT HIỆN: Tín hiệu SHORT hợp lệ!")
            execute_trade(sym_config, signal_candle, signal_candle['close'], "SHORT")
        elif is_signal_long:
            print(f"   ⚡ PHÁT HIỆN: Tín hiệu LONG hợp lệ!")
            execute_trade(sym_config, signal_candle, signal_candle['close'], "LONG")
        else:
            print(f"   - Không có tín hiệu nến phù hợp.")


def scheduled_task():
    # Tác vụ lập lịch chạy tự động mỗi 5 phút
    while True:
        now_utc = datetime.now(ZoneInfo("UTC"))
        if now_utc.minute % 5 == 0 and now_utc.second == 3:
            try:
                trading_bot_task() 
                check_and_cancel_stale_orders()
                manage_position_sl_to_entry() 
                
            except Exception as e:
                error_msg = f"LỖI NGHIÊM TRỌNG TRONG SCHEDULED TASK:\n{e}\n{traceback.format_exc()}"
                print(error_msg)
                send_slack_alert(f"```{error_msg}```", is_critical=True)
            finally:
                print("\n⏳ Chu kỳ hoàn tất, chờ 5 phút tiếp theo...")
                time.sleep(60)
        else:
            time.sleep(0.5)

# ==============================================================================
# ========== GIAO DIỆN VÀ KHỞI CHẠY ==========
# ==============================================================================

def run_manual_check():
    # Chạy kiểm tra thủ công
    threading.Thread(target=trading_bot_task).start()
    threading.Thread(target=check_and_cancel_stale_orders).start()
    threading.Thread(target=manage_position_sl_to_entry).start()
    return f"🟢 Đã kích hoạt kiểm tra thủ công lúc: {datetime.now(VIETNAM_TIMEZONE).strftime('%H:%M:%S')}"

def main():
    # Hàm khởi chạy chính
    print("🟢 Bot đang khởi chạy...")    
    scheduler_thread = threading.Thread(target=scheduled_task, daemon=True)
    scheduler_thread.start()
    print("✅ Tác vụ tự động đã được khởi chạy trong nền.")
    
    send_slack_alert("🤖 Bot giao dịch OKX đã khởi động (Chế độ LONG & SHORT).")
    
    with gr.Blocks(title="Trading Bot OKX") as demo:
        gr.Markdown("# 🤖 Trading Bot OKX - (Chế độ LONG & SHORT)")
        gr.Markdown(f"Bot tự động quét tín hiệu (LONG và SHORT) mỗi 5 phút.")
        
        status_output = gr.Textbox(label="Trạng thái", interactive=False, value="🟢 Bot đang chạy...")
        run_button = gr.Button("🔄 Chạy Kiểm Tra Thủ Công Ngay")
        run_button.click(fn=run_manual_check, outputs=status_output)
    
    demo.launch()

if __name__ == "__main__":
    main()