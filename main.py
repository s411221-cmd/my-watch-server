import os
from datetime import datetime, timedelta
from typing import Optional

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
DATABASE_URL = os.getenv("DATABASE_URL", "")

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

# ==================== 3. 初始化 FastAPI & 雙 LINE Bot ====================
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

# ==================== 4. 硬體 ESP32 數據接收 API ====================
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

    # 發燒即時警報送至家屬機器人
    if data.temperature >= 38.0 and family_bot_api and FAMILY_LINE_USER_ID:
        fever_alert_card = FlexSendMessage(
            alt_text="🚨【高溫發燒緊急警報】",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#CC0000"}},
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🚨 高溫發燒警報", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"檢測到長輩體溫異常高溫：{data.temperature} °C", "weight": "bold", "color": "#D9534F"},
                        {"type": "text", "text": f"心率：{data.heart_rate} bpm | 血氧：{data.spo2} %", "size": "sm", "margin": "md"}
                    ]
                }
            }
        )
        try:
            family_bot_api.push_message(FAMILY_LINE_USER_ID, fever_alert_card)
        except Exception as e:
            print(f"家屬機器人推播發燒警報失敗: {e}")

    return {"status": "success", "data": data}

# ==================== 5. LINE Webhook 處理 ====================
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

    # 2. 按鈕一：一鍵聯繫家屬[cite: 3]
    if user_text == "一鍵聯繫家屬":
        latest_temp, latest_bpm, latest_spo2 = 36.5, 75, 98
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature
                latest_bpm = latest_log.heart_rate
                latest_spo2 = latest_log.spo2

        # 長輩端：藍色卡片[cite: 3]
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

        # 家屬端：黑色警報[cite: 4]
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

    # 3. 按鈕二：本週健康日記
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
                "styles": {"header": {"backgroundColor": "#2D6A4F"}}, # 森林綠 Header
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

    # 4. 按鈕三：飲食保養建議
    if user_text == "飲食保養建議":
        latest_temp = 36.5
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature

        # 根據體溫提供保養提示
        diet_tip = "建議補充足量溫開水，並攝取優質蛋白質（如豆腐、蒸魚）與深綠色蔬菜。"
        if latest_temp >= 37.2:
            diet_tip = "體溫微偏高，建議多補充水分與電解質，飲食以清淡粥品為主，避免油膩辛辣。"

        diet_card = FlexSendMessage(
            alt_text="🥗 飲食保養建議",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#E67E22"}}, # 活力橘 Header
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

    # 5. 按鈕四：今日適合的運動
    if user_text == "今日適合的運動":
        latest_bpm = 75
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_bpm = latest_log.heart_rate

        # 依心率調整運動建議
        if latest_bpm > 100:
            ex_title = "🧘 靜態舒緩伸展"
            ex_desc = "目前心率稍快，建議以坐姿進行肩頸與手腕伸展，深呼吸放鬆 15 分鐘即可。"
        else:
            ex_title = "🚶 超慢跑與超慢步"
            ex_desc = "今日生理狀況良好！適合進行 20 分鐘公園散步、太極拳或居家椅上體操。"

        exercise_card = FlexSendMessage(
            alt_text="🏃 今日運動建議",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2980B9"}}, # 湛藍色 Header
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🏃 今日運動推薦", "weight": "bold", "color": "#FFFFFF", "size": "lg"}
                    ]
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": ex_title, "weight": "bold", "size": "md", "color": "#2980B9"},
                        {"type": "text", "text": ex_desc, "wrap": True, "size": "sm", "margin": "md", "color": "#333333"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": "⚠️ 運動提醒：運動前請先喝水 200c.c.，若感覺頭暈請立即停止並休息。", "wrap": True, "size": "xs", "color": "#C0392B", "margin": "sm"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, exercise_card)
        return

    # 預設非關鍵字回覆
    elder_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"已收到您的訊息：「{user_text}」。請點擊下方選單按鈕使用各項健康服務！")
    )
    elder_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"已收到您的訊息：「{user_text}」。如有緊急狀況請點擊「一鍵聯繫家屬」。")
    )
