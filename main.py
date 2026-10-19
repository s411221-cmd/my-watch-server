import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
)

# ==================== 1. 環境變數讀取與設定 ====================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
FAMILY_BOT_TOKEN = os.getenv("FAMILY_BOT_TOKEN", "")
FAMILY_LINE_USER_ID = os.getenv("FAMILY_LINE_USER_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Render 資料庫網址修正 (postgres:// 轉換為 postgresql://)
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

# A. 長輩端機器人 (Bot A)
elder_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# B. 家屬端機器人 (Bot B) - 若未填寫則自動 fallback 到長輩端機器人
family_bot_api = LineBotApi(FAMILY_BOT_TOKEN) if FAMILY_BOT_TOKEN else elder_bot_api

class TelemetryInput(BaseModel):
    temperature: float
    heart_rate: int
    spo2: int

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Health Care System Backend Active"}

# ==================== 4. 硬體 ESP32 上送生理數據 API ====================
@app.post("/api/v1/telemetry")
def receive_telemetry(data: TelemetryInput, db: Session = Depends(get_db)):
    # 寫入資料庫
    if db:
        log = HealthLog(
            temperature=data.temperature,
            heart_rate=data.heart_rate,
            spo2=data.spo2
        )
        db.add(log)
        db.commit()
        db.refresh(log)

    # 若發燒 (>= 38.0°C)，透過「家屬機器人」主動推播警報給家屬
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

# ==================== 5. LINE Webhook 接收與處理 ====================
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

    # 指令 1：查詢 User ID (除錯用)
    if user_text == "我的ID":
        elder_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"您的 LINE User ID 為：\n{user_id}")
        )
        return

    # 指令 2：一鍵聯繫家屬
    if user_text == "一鍵聯繫家屬":
        db = next(get_db()) if SessionLocal else None
        latest_temp = 36.5
        latest_bpm = 75
        latest_spo2 = 98

        # 優先讀取資料庫最新紀錄，避開 0.0°C 問題
        if db:
            latest_log = db.query(HealthLog).order_by(HealthLog.created_at.desc()).first()
            if latest_log:
                latest_temp = latest_log.temperature
                latest_bpm = latest_log.heart_rate
                latest_spo2 = latest_log.spo2

        # --- A. 長輩機器人 (elder_bot_api)：回覆藍色 Flex 卡片給長輩 ---
        elder_blue_card = FlexSendMessage(
            alt_text="已通知家屬",
            contents={
                "type": "bubble",
                "styles": {"header": {"backgroundColor": "#2B4C7E"}}, # 藍色風格 Header
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
                        {"type": "text", "text": "系統已將您的最新生理狀態同步傳送給家屬關懷機器人。", "wrap": True, "size": "sm", "color": "#555555"}
                    ]
                }
            }
        )
        elder_bot_api.reply_message(event.reply_token, elder_blue_card)

        # --- B. 家屬機器人 (family_bot_api)：主動推播黑色警報給家屬 ---
        if family_bot_api and FAMILY_LINE_USER_ID:
            family_black_card = FlexSendMessage(
                alt_text="🚨【長輩安危關懷通知】",
                contents={
                    "type": "bubble",
                    "styles": {"header": {"backgroundColor": "#1A1A1A"}}, # 黑色風格 Header
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
                            {"type": "text", "text": "長輩剛才按下了「一鍵聯繫家屬」請求關懷！", "weight": "bold", "size": "md"},
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

    # 預設回覆
    elder_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"已收到您的訊息：「{user_text}」。如有緊急狀況請點擊「一鍵聯繫家屬」。")
    )
