import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, DateTime, Boolean, func
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
ELDER_LINE_USER_ID = os.getenv("ELDER_LINE_USER_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ==================== 2. PostgreSQL 資料庫連線 (新增步數與跌倒) ====================
engine = create_engine(DATABASE_URL) if DATABASE_URL else None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None
Base = declarative_base()

class HealthLog(Base):
    __tablename__ = "health_logs"
    id = Column(Integer, primary_key=True, index=True)
    temperature = Column(Float, nullable=False)
    heart_rate = Column(Integer, nullable=False)
    spo2 = Column(Integer, nullable=False)
    step_count = Column(Integer, default=0)         # 新增：今日累積步數
    is_fall = Column(Boolean, default=False)        # 新增：是否跌倒
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


def generate_ai_exercise_advice(bpm: int, temp: float, weather_info: dict, steps: int = 0) -> tuple[str, str]:
    w_temp = weather_info["temp"]
    w_cond = weather_info["condition"]
    is_rainy = weather_info["is_rainy"]

    if OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            prompt = f"""
            你是一位銀髮族健康與運動專家。請根據長輩目前的生理數據、今日步數與戶外天氣，給予今日最安全的運動建議。
            - 長輩最新心率：{bpm} bpm | 體溫：{temp} °C
            - 今日已走步數：{steps} 步
            - 當前天氣：{w_cond}，戶外氣溫 {w_temp}°C，{'正下著雨' if is_rainy else '未下雨'}

            請回傳 JSON 格式：
            {{
              "title": "運動主題",
              "description": "建議內容 (100字以內)"
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
        desc = f"戶外雨天（{w_temp}°C）路滑易摔。目前累積 {steps} 步，建議今日在室內進行抬腿與椅上拉筋 15 分鐘。"
    elif steps >= 6000:
        title = "🧘 靜態伸展與腿部按摩"
        desc = f"太棒了！今日已累積走達 {steps} 步，運動量充沛。建議今晚進行靜態拉筋與腿部放鬆，避免肌肉痠痛。"
    else:
        title = "🚶 公園散步與超慢跑"
        desc = f"今日天氣{w_cond}（{w_temp}°C），目前已走 {steps} 步。建議可至附近公園再散步 20 分鐘達到目標！"

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
    step_count: Optional[int] = 0
    is_fall: Optional[bool] = False

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Health Care System Backend Active"}

# ==================== 5. 數據接收 API (含跌倒與多重警報) ====================
@app.post("/api/v1/telemetry")
def receive_telemetry(data: TelemetryInput, db: Session = Depends(get_db)):
    if db:
        log = HealthLog(
            temperature=data.temperature,
            heart_rate=data.heart_rate,
            spo2=data.spo2,
            step_count=data.step_count,
            is_fall=data.is_fall
        )
        db.add(log)
        db.commit()
        db.refresh(log)

    # 1. 最高優先級：突發跌倒緊急警報
    if data.is_fall:
        fall_card = FlexSendMessage(
            alt_text="🚨🚨【突發跌倒緊急警報】",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#8B0000"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🚨🚨 突發跌倒警報", "weight": "bold", "color": "#FFFFFF", "size": "xl"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "⚠️ 偵測到長輩發生疑似摔倒/跌倒衝擊！", "weight": "bold", "color": "#CC0000", "size": "md"},
                        {"type": "text", "text": "請立即確認長輩狀況，必要時請撥打 119！", "size": "sm", "margin": "sm", "color": "#333333"},
                        {"type": "separator", "margin": "md"},
                        {
                            "type": "text", 
                            "text": f"📊 當前生理即時數據：\n• 體溫：{data.temperature} °C\n• 心率：{data.heart_rate} bpm\n• 血氧：{data.spo2} %\n• 今日累積步數：{data.step_count} 步", 
                            "wrap": True, "size": "xs", "color": "#555555", "margin": "md"
                        }
                    ]
                }
            }
        )
        if family_bot_api and FAMILY_LINE_USER_ID:
            try: family_bot_api.push_message(FAMILY_LINE_USER_ID, fall_card)
            except Exception as e: print(f"家屬跌倒推播失敗: {e}")

        if elder_bot_api:
            try:
                if ELDER_LINE_USER_ID: elder_bot_api.push_message(ELDER_LINE_USER_ID, fall_card)
                else: elder_bot_api.broadcast(fall_card)
            except Exception as e: print(f"長輩跌倒推播失敗: {e}")

        return {"status": "alert_sent", "type": "fall_detected"}

    # 2. 一般生理數據異常警報 (體溫、血氧、心率)
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
        alert_reasons.append(f"💓 心率異常：{data.heart_rate} bpm")

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
                        {"type": "text", "text": reasons_text, "wrap": True, "size": "sm", "weight": "bold", "color": "#CC0000"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": f"📊 當前數據：體溫 {data.temperature}°C | 心率 {data.heart_rate}bpm | 血氧 {data.spo2}% | 步數 {data.step_count} 步", "wrap": True, "size": "xs", "color": "#555555", "margin": "md"}
                    ]
                }
            }
        )
        if family_bot_api and FAMILY_LINE_USER_ID:
            try: family_bot_api.push_message(FAMILY_LINE_USER_ID, alert_card)
            except Exception as e: pass

        if elder_bot_api:
            try:
                if ELDER_LINE_USER_ID: elder_bot_api.push_message(ELDER_LINE_USER_ID, alert_card)
                else: elder_bot_api.broadcast(alert_card)
            except Exception as e: pass

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

    if user_text == "我的ID":
        elder_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"您的 LINE User ID 為：\n{user_id}"))
        return

    # 一鍵聯繫家屬
    if user_text == "一鍵聯繫家屬":
        latest_temp, latest_bpm, latest_spo2, latest_steps = 36.5, 75, 98, 0
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature
                latest_bpm = latest_log.heart_rate
                latest_spo2 = latest_log.spo2
                latest_steps = latest_log.step_count

        elder_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text="已通知家屬",
                contents={
                    "type": "bubble",
                    "styles": {"header": {"backgroundColor": "#2B4C7E"}},
                    "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "已成功通知家屬", "weight": "bold", "color": "#FFFFFF", "size": "lg"}]},
                    "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "系統已將您的緊急求助傳送給家屬機器人。", "size": "sm"}]}
                }
            )
        )

        if family_bot_api and FAMILY_LINE_USER_ID:
            family_bot_api.push_message(
                FAMILY_LINE_USER_ID,
                FlexSendMessage(
                    alt_text="🚨【長輩安危關懷通知】",
                    contents={
                        "type": "bubble",
                        "styles": {"header": {"backgroundColor": "#1A1A1A"}},
                        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🚨【長輩安危關懷通知】", "weight": "bold", "color": "#FF4D4D", "size": "lg"}]},
                        "body": {
                            "type": "box", "layout": "vertical",
                            "contents": [
                                {"type": "text", "text": "長輩按下「一鍵聯繫家屬」求助按鈕！", "weight": "bold", "size": "md"},
                                {"type": "text", "text": f"• 體溫：{latest_temp} °C\n• 心率：{latest_bpm} bpm\n• 血氧：{latest_spo2} %\n• 今日步數：{latest_steps} 步", "size": "sm", "margin": "md"}
                            ]
                        }
                    }
                )
            )
        return

    # 本週健康日記 (帶出總步數)
    if user_text == "本週健康日記":
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        avg_temp, avg_bpm, avg_spo2, max_steps, log_count = 36.5, 75, 98, 0, 0

        if db:
            logs = db.query(
                func.avg(HealthLog.temperature).label("avg_temp"),
                func.avg(HealthLog.heart_rate).label("avg_bpm"),
                func.avg(HealthLog.spo2).label("avg_spo2"),
                func.max(HealthLog.step_count).label("max_steps"),
                func.count(HealthLog.id).label("count")
            ).filter(HealthLog.created_at >= seven_days_ago).first()

            if logs and logs.count > 0:
                avg_temp = round(logs.avg_temp, 1)
                avg_bpm = int(logs.avg_bpm)
                avg_spo2 = int(logs.avg_spo2)
                max_steps = logs.max_steps or 0
                log_count = logs.count

        weekly_card = FlexSendMessage(
            alt_text="📊 本週健康日記",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2D6A4F"}},
                "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "📊 近 7 天健康與運動週報", "weight": "bold", "color": "#FFFFFF", "size": "lg"}]},
                "body": {
                    "type": "box", "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"平均體溫：{avg_temp} °C", "size": "sm", "weight": "bold"},
                        {"type": "text", "text": f"平均心率：{avg_bpm} bpm", "size": "sm", "weight": "bold", "margin": "xs"},
                        {"type": "text", "text": f"平均血氧：{avg_spo2} %", "size": "sm", "weight": "bold", "margin": "xs"},
                        {"type": "text", "text": f"今日最新步數：{max_steps} 步", "size": "sm", "weight": "bold", "color": "#1B4332", "margin": "xs"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": "💡 評語：活動量適中，生理指標良好！", "wrap": True, "size": "xs", "color": "#2D6A4F", "margin": "md"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, weekly_card)
        return

    # 今日適合的運動 (結合步數 + 天氣 + AI)
    if user_text == "今日適合的運動":
        latest_bpm, latest_temp, latest_steps = 75, 36.5, 0
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_bpm = latest_log.heart_rate
                latest_temp = latest_log.temperature
                latest_steps = latest_log.step_count

        weather_info = get_taiwan_weather()
        ex_title, ex_desc = generate_ai_exercise_advice(latest_bpm, latest_temp, weather_info, latest_steps)

        exercise_card = FlexSendMessage(
            alt_text="🏃 今日運動建議",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2980B9"}},
                "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🏃 AI 天氣與步數運動建議", "weight": "bold", "color": "#FFFFFF", "size": "lg"}]},
                "body": {
                    "type": "box", "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"🌤️ 天氣：{weather_info['condition']} ({weather_info['temp']}°C) | 👟 步數：{latest_steps} 步", "size": "xs", "color": "#666666"},
                        {"type": "separator", "margin": "sm"},
                        {"type": "text", "text": ex_title, "weight": "bold", "size": "md", "color": "#2980B9", "margin": "md"},
                        {"type": "text", "text": ex_desc, "wrap": True, "size": "sm", "margin": "md", "color": "#333333"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, exercise_card)
        return

    elder_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"已收到：「{user_text}」"))
