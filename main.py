import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
)

# ==================== 1. 環境變數讀取 ====================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
FAMILY_BOT_TOKEN = os.getenv("FAMILY_BOT_TOKEN", "")
FAMILY_LINE_USER_ID = os.getenv("FAMILY_LINE_USER_ID", "")
ELDER_LINE_USER_ID = os.getenv("ELDER_LINE_USER_ID", "") # 長輩/使用者的 LINE User ID (可選)
DATABASE_URL = os.getenv("DATABASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ==================== 2. PostgreSQL 資料庫連線 ====================
engine = create_engine(DATABASE_URL) if DATABASE_URL else None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None
Base = declarative_base()

class HealthLog(Base):
    __tablename__ = "health_logs"
    id = Column(Integer, primary_key=True, index=True)
    temperature = Column(Float, nullable=False)
    heart_rate = Column(Integer, nullable=False)
    spo2 = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

if engine:
    Base.metadata.create_all(bind=engine)

def get_db():
    if not SessionLocal:
        yield None
        return
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==================== 3. 天氣與 AI 運動建議模組 ====================
def get_taiwan_weather():
    """抓取即時天氣狀況 (免 API Key，預設台北/台灣區域座標)"""
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=25.0375&longitude=121.5637&current_weather=true"
        res = requests.get(url, timeout=4)
        if res.status_code == 200:
            data = res.json().get("current_weather", {})
            temp = data.get("temperature", 24.0)
            code = data.get("weathercode", 0)
            
            if code == 0:
                cond, is_rain = "晴朗", False
            elif code in [1, 2, 3]:
                cond, is_rain = "多雲/晴時多雲", False
            elif code >= 50:
                cond, is_rain = "陰雨/降雨", True
            else:
                cond, is_rain = "陰天", False

            return {"temp": temp, "condition": cond, "is_rainy": is_rain}
    except Exception as e:
        print(f"Weather Fetch Error: {e}")
    return {"temp": 25.0, "condition": "舒適", "is_rainy": False}


def generate_ai_exercise_advice(bpm: int, temp: float, weather_info: dict) -> tuple[str, str]:
    """結合長輩生理數據與天氣狀況，由 AI 判定運動建議"""
    w_temp = weather_info["temp"]
    w_cond = weather_info["condition"]
    is_rainy = weather_info["is_rainy"]

    if OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            prompt = f"""
            你是一位銀髮族健康與運動專家。請根據長輩目前的生理數據與戶外天氣，給予今日最安全的運動建議。
            - 長輩最新心率：{bpm} bpm
            - 長輩最新體溫：{temp} °C
            - 當前天氣：{w_cond}，戶外氣溫 {w_temp}°C，{'正下著雨' if is_rainy else '未下雨'}

            請回傳 JSON 格式：
            {{
              "title": "運動主題 (例如：🏠 室內椅上舒緩伸展 / 🚶 陰涼處公園超慢跑)",
              "description": "建議內容 (100字以內，說明推薦原因、合適時間與注意事項)"
            }}
            """
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            import json
            result = json.loads(response.choices[0].message.content)
            return result.get("title", "🏃 今日運動推薦"), result.get("description", "")
        except Exception as e:
            print(f"OpenAI API Call Failed: {e}")

    if is_rainy:
        title = "🏠 室內客廳椅上運動"
        desc = f"戶外目前有雨（{w_temp}°C），路面濕滑易摔倒。建議今日在室內進行 15 分鐘抬腿與椅上拉筋，促進血液循環又安全。"
    elif w_temp >= 30.0:
        title = "❄️ 晨間/傍晚清涼散步"
        desc = f"今日戶外氣溫高達 {w_temp}°C 較為炎熱。建議避開中午高溫，改於清晨或傍晚至陰涼處散步 20 分鐘，並請記得多補充水分。"
    elif w_temp <= 18.0:
        title = "🧥 溫暖室內毛巾操"
        desc = f"今日戶外氣溫偏低（僅 {w_temp}°C），血管容易收縮。建議穿足保暖衣物，在室內進行 15 分鐘毛巾操或暖身伸展。"
    elif bpm > 100:
        title = "🧘 靜態腹式呼吸放鬆"
        desc = f"雖然天氣{w_cond}（{w_temp}°C）很適合出門，但檢測到您目前心率稍微偏快（{bpm} bpm）。今日請以靜態深呼吸與手部伸展為主。"
    else:
        title = "🚶 公園超慢跑與散步"
        desc = f"今日天氣{w_cond}、氣溫 {w_temp}°C 非常舒適！您的心率也相當穩定（{bpm} bpm），非常適合至附近公園散步或超慢跑 20~30 分鐘。"

    return title, desc


# ==================== 4. 初始化 FastAPI & 雙 LINE Bot ====================
app = FastAPI(title="Elderly Health Care System")

elder_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None
family_bot_api = LineBotApi(FAMILY_BOT_TOKEN) if FAMILY_BOT_TOKEN else elder_bot_api

class TelemetryInput(BaseModel):
    temperature: float
    heart_rate: int
    spo2: int

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Health Care System Backend Active"}

# ==================== 5. 硬體 ESP32 數據接收 API (含雙機器人多重警報推播) ====================
@app.post("/api/v1/telemetry")
def receive_telemetry(data: TelemetryInput, db: Session = Depends(get_db)):
    if db:
        log = HealthLog(
            temperature=data.temperature,
            heart_rate=data.heart_rate,
            spo2=data.spo2
        )
        db.add(log)
        db.commit()
        db.refresh(log)

    # A. 多重異常條件判定 (體溫、心率、血氧)
    is_alert = False
    alert_reasons = []

    if data.temperature >= 38.0:
        is_alert = True
        alert_reasons.append(f"🔥 體溫過高：{data.temperature} °C (高燒)")
    elif data.temperature > 0 and data.temperature < 35.0:
        is_alert = True
        alert_reasons.append(f"❄️ 體溫過低：{data.temperature} °C")

    if 0 < data.spo2 < 90:
        is_alert = True
        alert_reasons.append(f"🫁 血氧過低：{data.spo2} % (低血氧)")

    if data.heart_rate > 0 and (data.heart_rate > 120 or data.heart_rate < 50):
        is_alert = True
        if data.heart_rate > 120:
            alert_reasons.append(f"💓 心率過快：{data.heart_rate} bpm")
        else:
            alert_reasons.append(f"💓 心率過慢：{data.heart_rate} bpm")

    # B. 觸發警報時，同步推播給「家屬機器人」與「使用者/長輩機器人」
    if is_alert:
        reasons_text = "\n".join(alert_reasons)
        alert_card = FlexSendMessage(
            alt_text="🚨【健康異常緊急警報】",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#CC0000"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🚨 生理數據危急警報", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "檢測到生理指標異常：", "weight": "bold", "color": "#D9534F", "size": "sm"},
                        {"type": "text", "text": reasons_text, "wrap": True, "size": "sm", "margin": "md", "weight": "bold", "color": "#CC0000"},
                        {"type": "separator", "margin": "md"},
                        {
                            "type": "text", 
                            "text": f"📊 當前數據概況：\n• 體溫：{data.temperature} °C\n• 心率：{data.heart_rate} bpm\n• 血氧：{data.spo2} %", 
                            "wrap": True, "size": "xs", "color": "#555555", "margin": "md"
                        },
                        {"type": "text", "text": "⚠️ 請關注使用者身體狀況，必要時尋求醫療協助！", "wrap": True, "size": "xs", "color": "#CC0000", "margin": "md", "weight": "bold"}
                    ]
                }
            }
        )

        # 1. 發送給家屬機器人
        if family_bot_api and FAMILY_LINE_USER_ID:
            try:
                family_bot_api.push_message(FAMILY_LINE_USER_ID, alert_card)
            except Exception as e:
                print(f"家屬機器人推播警報失敗: {e}")

        # 2. 發送給長輩/使用者機器人
        if elder_bot_api:
            try:
                if ELDER_LINE_USER_ID:
                    elder_bot_api.push_message(ELDER_LINE_USER_ID, alert_card)
                else:
                    elder_bot_api.broadcast(alert_card) # 若無特指個別 User ID 則廣播發送
            except Exception as e:
                print(f"使用者機器人推播警報失敗: {e}")

    return {"status": "success", "data": data}

# ==================== 6. LINE Webhook 處理 ====================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id
    db = next(get_db()) if SessionLocal else None

    # 1. 查詢 User ID
    if user_text == "我的ID":
        elder_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"您的 LINE User ID 為：\n{user_id}")
        )
        return

    # 2. 一鍵聯繫家屬
    if user_text == "一鍵聯繫家屬":
        latest_temp, latest_bpm, latest_spo2 = 36.5, 75, 98
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature
                latest_bpm = latest_log.heart_rate
                latest_spo2 = latest_log.spo2

        # 長輩端：藍色卡片
        elder_blue_card = FlexSendMessage(
            alt_text="已通知家屬",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2B4C7E"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "已成功通知家屬", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "系統已將您的緊急通知傳送給家屬關懷機器人。", "wrap": True, "size": "sm", "color": "#555555"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, elder_blue_card)

        # 家屬端：黑色警報
        if family_bot_api and FAMILY_LINE_USER_ID:
            family_black_card = FlexSendMessage(
                alt_text="🚨【長輩安危關懷通知】",
                contents={
                    "type": "bubble",
                    "styles": {"header": {"backgroundColor": "#1A1A1A"}},
                    "header": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "🚨【長輩安危關懷通知】", "weight": "bold", "color": "#FF4D4D", "size": "lg"}
                        ]
                    },
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "長輩剛才按下了「一鍵聯繫家屬」！", "weight": "bold", "size": "md"},
                            {"type": "text", "text": f"目前體溫：{latest_temp} °C", "size": "sm", "margin": "md"},
                            {"type": "text", "text": f"目前心率：{latest_bpm} bpm", "size": "sm"},
                            {"type": "text", "text": f"目前血氧：{latest_spo2} %", "size": "sm"}
                        ]
                    }
                }
            )
            try:
                family_bot_api.push_message(FAMILY_LINE_USER_ID, family_black_card)
            except Exception as e:
                print(f"家屬機器人推播失敗: {e}")
        return

    # 3. 本週健康日記
    if user_text == "本週健康日記":
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        avg_temp, avg_bpm, avg_spo2, log_count = 36.5, 75, 98, 0

        if db:
            logs = db.query(
                func.avg(HealthLog.temperature).label("avg_temp"),
                func.avg(HealthLog.heart_rate).label("avg_bpm"),
                func.avg(HealthLog.spo2).label("avg_spo2"),
                func.count(HealthLog.id).label("count")
            ).filter(HealthLog.created_at >= seven_days_ago).first()

            if logs and logs.count > 0:
                avg_temp = round(logs.avg_temp, 1)
                avg_bpm = int(logs.avg_bpm)
                avg_spo2 = int(logs.avg_spo2)
                log_count = logs.count

        weekly_card = FlexSendMessage(
            alt_text="📊 本週健康日記",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2D6A4F"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "📊 近 7 天健康週報", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"總量測次數：{log_count} 次", "size": "xs", "color": "#888888"},
                        {"type": "separator", "margin": "sm"},
                        {"type": "text", "text": f"平均體溫：{avg_temp} °C", "size": "sm", "margin": "md", "weight": "bold"},
                        {"type": "text", "text": f"平均心率：{avg_bpm} bpm", "size": "sm", "margin": "xs", "weight": "bold"},
                        {"type": "text", "text": f"平均血氧：{avg_spo2} %", "size": "sm", "margin": "xs", "weight": "bold"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": "💡 評語：本週生理指標維持良好，請繼續保持規律作息！", "wrap": True, "size": "xs", "color": "#2D6A4F", "margin": "md"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, weekly_card)
        return

    # 4. 飲食保養建議
    if user_text == "飲食保養建議":
        latest_temp = 36.5
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature

        diet_tip = "建議補充足量溫開水，並攝取優質蛋白質（如豆腐、蒸魚）與深綠色蔬菜。"
        if latest_temp >= 37.2:
            diet_tip = "體溫微偏高，建議多補充水分與電解質，飲食以清淡粥品為主，避免油膩辛辣。"

        diet_card = FlexSendMessage(
            alt_text="🥗 飲食保養建議",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#E67E22"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🥗 今日飲食保養建議", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "【長輩專屬膳食指南】", "weight": "bold", "size": "md", "color": "#E67E22"},
                        {"type": "text", "text": diet_tip, "wrap": True, "size": "sm", "margin": "md", "color": "#333333"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": "🥛 每日水分建議量：約 1500 - 2000 c.c.", "size": "xs", "color": "#666666", "margin": "sm"},
                        {"type": "text", "text": "🍎 飯後可搭配一份少糖水果（如蘋果、芭樂）", "size": "xs", "color": "#666666", "margin": "xs"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, diet_card)
        return

    # 5. 今日適合的運動 (整合即時天氣 + AI 智慧判斷)
    if user_text == "今日適合的運動":
        latest_bpm = 75
        latest_temp = 36.5
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_bpm = latest_log.heart_rate
                latest_temp = latest_log.temperature

        weather_info = get_taiwan_weather()
        ex_title, ex_desc = generate_ai_exercise_advice(latest_bpm, latest_temp, weather_info)

        exercise_card = FlexSendMessage(
            alt_text="🏃 今日運動建議 (天氣+AI分析)",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2980B9"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🏃 AI 天氣運動建議", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": f"🌤️ 天氣：{weather_info['condition']}", "size": "xs", "color": "#666666"},
                                {"type": "text", "text": f"🌡️ 氣溫：{weather_info['temp']}°C", "size": "xs", "color": "#666666", "align": "end"}
                            ]
                        },
                        {"type": "separator", "margin": "sm"},
                        {"type": "text", "text": ex_title, "weight": "bold", "size": "md", "color": "#2980B9", "margin": "md"},
                        {"type": "text", "text": ex_desc, "wrap": True, "size": "sm", "margin": "md", "color": "#333333"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": f"📊 生理參考：心率 {latest_bpm} bpm | 體溫 {latest_temp}°C", "size": "xs", "color": "#888888", "margin": "sm"},
                        {"type": "text", "text": "⚠️ 提醒：請依個人體力調整，如有頭暈不適請立即休息。", "wrap": True, "size": "xs", "color": "#C0392B", "margin": "xs"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, exercise_card)
        return

    elder_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"已收到您的訊息：「{user_text}」。請點擊下方選單按鈕使用各項健康服務！")
    )
