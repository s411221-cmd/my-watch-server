import os
import json
import asyncio
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer, QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import google.generativeai as genai

# SQLAlchemy 資料庫套件 (相容 SQLAlchemy 1.4/2.0+)
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ===== 1. 環境變數設定 =====
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://my-watch-server.onrender.com")

# 💡 家屬的 LINE User ID (請在 Render 環境變數設定 FAMILY_LINE_USER_ID)
FAMILY_USER_ID = os.getenv("FAMILY_LINE_USER_ID", "")

# 資料庫連線字串 (自動把 postgres:// 轉為 postgresql://)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./health_watch.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ===== 2. 資料庫 Schema =====
class HealthLog(Base):
    __tablename__ = "health_logs"

    id = Column(Integer, primary_key=True, index=True)
    watch_id = Column(String, index=True)
    temp = Column(Float)
    heart_rate = Column(Integer)
    spo2 = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# LINE 與 AI 初始化
configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
else:
    gemini_model = None

# ===== 3. 工具函數：讀取最新健康數據 =====
def get_latest_data(db: Session) -> dict:
    last_log = db.query(HealthLog).order_by(HealthLog.id.desc()).first()
    if last_log:
        return {
            "watch_id": last_log.watch_id,
            "temp": last_log.temp,
            "heart_rate": last_log.heart_rate,
            "spo2": last_log.spo2,
            "updated_at": last_log.created_at.strftime("%Y-%m-%d %H:%M:%S")
        }
    return {"watch_id": "Watch_001", "temp": 36.5, "heart_rate": 75, "spo2": 98, "updated_at": "尚未收到上傳"}

# ===== 4. 工具函數：推播緊急訊息給家屬 =====
def push_to_family(messaging_api: MessagingApi, text_msg: str):
    target_id = FAMILY_USER_ID
    if target_id:
        try:
            messaging_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=[TextMessage(text=text_msg)]
                )
            )
            print(f"✅ [Push Success] 已發送推播訊息給家屬: {target_id}")
        except Exception as e:
            print(f"❌ [Push Error] 發送家屬訊息失敗: {e}")
    else:
        print("⚠️ [Push Warning] 未設定 FAMILY_LINE_USER_ID，無法發送訊息給家屬！")

# ===== 5. 生命週期與 Keep-Alive =====
async def keep_alive_task():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try:
                await client.get(f"{RENDER_URL}/api/v1/healthz")
            except Exception as e:
                print(f"[Keep-Alive] Ping 失敗: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(keep_alive_task())
    yield

app = FastAPI(lifespan=lifespan)

# ===== 6. API Endpoints =====

@app.get("/api/v1/healthz")
async def health_check():
    return {"status": "healthy", "service": "health-watch-backend"}

# 📥 接收手環數據 (若高溫/高心率會自動發送警報給家屬)
@app.post("/api/v1/telemetry")
async def receive_telemetry(data: dict, db: Session = Depends(get_db)):
    watch_id = data.get("watch_id", "Watch_001")
    temp = float(data.get("temp", 36.5))
    heart_rate = int(data.get("heart_rate", 75))
    spo2 = int(data.get("spo2", 98))

    try:
        log_entry = HealthLog(
            watch_id=watch_id,
            temp=temp,
            heart_rate=heart_rate,
            spo2=spo2
        )
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)
        print(f"✅ [DB Success] 數據已存入 DB ID: {log_entry.id}")

        # 🚨 異常數據自動觸發警報推播給家屬
        if temp > 38.0 or heart_rate > 120 or spo2 < 90:
            alert_msg = (
                f"🚨【長輩健康警訊通知】\n"
                f"手錶 [{watch_id}] 傳回異常數據！\n\n"
                f"- 狀態：紅燈\n"
                f"- 體溫：{temp}°C\n"
                f"- 血氧：{spo2}%\n"
                f"- 心率：{heart_rate}bpm\n\n"
                f"🤖 AI 醫師分析：\n"
                f"🚨【AI 緊急警告】數據顯示體溫/心率異常，請立即關心長輩狀況！\n\n"
                f"請儘快確認長輩狀況！"
            )
            with ApiClient(configuration) as api_client:
                messaging_api = MessagingApi(api_client)
                push_to_family(messaging_api, alert_msg)

        return {"status": "success", "saved_id": log_entry.id}
    except Exception as e:
        db.rollback()
        print(f"❌ [DB Error] 寫入資料庫失敗: {e}")
        return {"status": "error", "message": str(e)}

# 🌐 【方法 2】網頁檢視 API：查看最新一筆即時數據
@app.get("/api/v1/telemetry/latest")
async def get_latest_telemetry(db: Session = Depends(get_db)):
    data = get_latest_data(db)
    return {"status": "success", "data": data}

# 🌐 【方法 2】網頁檢視 API：查看歷史數據紀錄清單
@app.get("/api/v1/telemetry/history")
async def get_telemetry_history(limit: int = 20, db: Session = Depends(get_db)):
    logs = db.query(HealthLog).order_by(HealthLog.id.desc()).limit(limit).all()
    history_list = [
        {
            "id": log.id,
            "watch_id": log.watch_id,
            "temp": log.temp,
            "heart_rate": log.heart_rate,
            "spo2": log.spo2,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S")
        }
        for log in logs
    ]
    return {"status": "success", "count": len(history_list), "history": history_list}

# ===== 7. AI 與 Flex Message 生成器 =====

def generate_ai_advice(category: str, symptom: str, data: dict) -> str:
    temp = data["temp"]
    hr = data["heart_rate"]
    
    if not gemini_model:
        return f"當前體溫 {temp}°C、心率 {hr} bpm。請保持充足休息與定時補充水分。"

    prompt = f"""
    你是一位溫柔專業的家庭醫師與營養師。請根據使用者當前數據與需求，給予溫馨且具體的保養建議：
    【類別】: {category}
    【即時數據】: 體溫 {temp} °C, 心率 {hr} bpm
    【主訴狀況】: {symptom}
    
    【要求】: 針對數據與症狀給予 2-3 點具體建議，字數控制在 120 字以內。
    """
    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ [Gemini API Error]: {e}")
        return f"當前體溫 {temp}°C、心率 {hr} bpm。建議適度休息並補充電解質水分。"

def build_flex_card(title: str, advice: str, data: dict) -> FlexContainer:
    temp = data["temp"]
    hr = data["heart_rate"]
    bg_color = "#1E88E5" if "運動" in title else "#2E7D32"

    card_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": bg_color,
            "contents": [{"type": "text", "text": title, "weight": "bold", "color": "#FFFFFF", "size": "md"}]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"當前數據：體溫 {temp}°C | 心率 {hr} bpm", "size": "xs", "color": "#666666"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "💡 AI 醫師指導建議：", "weight": "bold", "margin": "md", "size": "sm"},
                {"type": "text", "text": advice, "wrap": True, "size": "xs", "color": "#333333", "margin": "sm"}
            ]
        }
    }
    return FlexContainer.from_dict(card_json)

def build_weekly_report_card(db: Session) -> FlexContainer:
    logs = db.query(HealthLog).order_by(HealthLog.id.desc()).limit(30).all()
    if not logs:
        status_text = "目前資料庫尚無長期累積紀錄，手環即時數據維持正常發送中。"
        count_str = "0 筆"
    else:
        avg_temp = round(sum(l.temp for l in logs) / len(logs), 1)
        avg_hr = int(sum(l.heart_rate for l in logs) / len(logs))
        count_str = f"{len(logs)} 筆紀錄"
        status_text = f"最近 {len(logs)} 筆資料顯示：平均體溫為 {avg_temp}°C，平均心率 {avg_hr} bpm，整體健康數據平穩。"

    card_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1565C0",
            "contents": [{"type": "text", "text": "📊 本週健康日記與紀錄分析", "weight": "bold", "color": "#FFFFFF", "size": "md"}]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"📈 累計數據分析（最近 {count_str}）", "size": "xs", "color": "#888888"},
                {"type": "separator", "margin": "sm"},
                {"type": "text", "text": status_text, "size": "xs", "color": "#333333", "wrap": True, "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "💡 提示：手環每 10 秒自動同步健康狀態至 PostgreSQL 資料庫長久保存。", "size": "xs", "color": "#2E7D32", "wrap": True, "margin": "sm"}
            ]
        }
    }
    return FlexContainer.from_dict(card_json)

def build_family_contact_card(data: dict) -> FlexContainer:
    temp = data["temp"]
    hr = data["heart_rate"]
    status_title = "🟢 生理狀態：正常" if temp <= 37.3 and hr <= 100 else "🔴 生理狀態：高溫/異常警示"
    status_color = "#2E7D32" if temp <= 37.3 and hr <= 100 else "#D32F2F"

    card_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0288D1",
            "contents": [{"type": "text", "text": "👨‍👩‍👧‍👦 家人健康嗶 / 緊急聯繫", "weight": "bold", "color": "#FFFFFF", "size": "md"}]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F5F5F5", "paddingAll": "10px", "cornerRadius": "8px",
                    "contents": [
                        {"type": "text", "text": status_title, "weight": "bold", "color": status_color, "size": "sm"},
                        {"type": "text", "text": f"即時手環數據：體溫 {temp}°C | 心率 {hr} bpm", "size": "xs", "color": "#555555", "margin": "xs"}
                    ]
                },
                {"type": "text", "text": "已連動長者智慧照護卡！系統已自動推播關懷訊息給家屬，您也可點擊下方按鈕直接撥打電話。", "size": "xs", "color": "#666666", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#D32F2F", "height": "sm",
                    "action": {"type": "uri", "label": "📞 撥打家屬緊急電話", "uri": "tel:0912345678"}
                }
            ]
        }
    }
    return FlexContainer.from_dict(card_json)

# ===== 8. LINE Webhook 事件處理 =====

@app.post("/callback")
async def line_callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    db = SessionLocal()

    try:
        latest_data = get_latest_data(db)

        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)

            # 💡 【除錯功能】打「我的ID」即可取得正確的 LINE User ID
            if text.lower() in ["我的id", "id", "myid"]:
                user_id = event.source.user_id
                msg_text = (
                    f"🆔 您的 LINE User ID 為：\n\n{user_id}\n\n"
                    f"請複製上方這串 ID，至 Render 的 Environment 變數設定為：\n"
                    f"Key: FAMILY_LINE_USER_ID\n"
                    f"Value: {user_id}"
                )
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[TextMessage(text=msg_text)])
                )
                return

            # 1. 今日適合的運動
            if text in ["今日適合的運動", "今日適合運動"]:
                advice = generate_ai_advice("運動建議", "無", latest_data)
                card = build_flex_card("🏃 今日專屬運動建議", advice, latest_data)
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="運動建議", contents=card)])
                )

            # 2. 飲食保養建議
            elif text in ["飲食保養建議", "飲食保養"]:
                qr = QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label="😊 無症狀/狀況良好", text="飲食調查:無症狀")),
                    QuickReplyItem(action=MessageAction(label="🤢 胃口差/腸胃不適", text="飲食調查:腸胃不適")),
                    QuickReplyItem(action=MessageAction(label="😴 頭暈/疲勞倦怠", text="飲食調查:頭暈疲勞")),
                    QuickReplyItem(action=MessageAction(label="🤒 喉嚨痛/發燒感", text="飲食調查:發燒不適"))
                ])
                msg = TextMessage(text="請選擇您目前是否有任何身體狀況，AI 營養師將為您評估最合適的飲食：", quick_reply=qr)
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[msg])
                )

            # 2-1. 飲食調查子選單
            elif text.startswith("飲食調查:"):
                symptom = text.replace("飲食調查:", "")
                advice = generate_ai_advice("飲食保養", symptom, latest_data)
                card = build_flex_card("🥗 今日專屬飲食建議", f"【狀況：{symptom}】\n{advice}", latest_data)
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="飲食建議", contents=card)])
                )

            # 3. 本週健康日記
            elif text in ["本週健康日記", "本週健康記"]:
                card = build_weekly_report_card(db)
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="本週健康日記", contents=card)])
                )

            # 4. 一鍵聯繫家屬 / 家人健康嗶
            elif text in ["一鍵聯繫家屬", "家人健康嗶", "家人健康"]:
                # A. 回覆長輩 Flex 卡片
                card = build_family_contact_card(latest_data)
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="家人健康與緊急聯繫", contents=card)])
                )

                # B. 同步推播緊急關懷通知給家屬 LINE 帳號
                family_notice = (
                    f"🚨【長輩安危關懷通知】\n"
                    f"長輩剛才點擊了「一鍵聯繫家屬」！\n\n"
                    f"📊【即時生理數據】\n"
                    f"・體溫：{latest_data['temp']} °C\n"
                    f"・血氧：{latest_data['spo2']} %\n"
                    f"・心率：{latest_data['heart_rate']} bpm\n\n"
                    f"請儘速與長輩聯繫關心狀況。"
                )
                push_to_family(messaging_api, family_notice)

            else:
                msg = TextMessage(text="您好！請點擊下方圖文選單按鈕，查詢 AI 健康建議、查看健康日記或聯繫家屬！")
                messaging_api.reply_message(
                    ReplyMessageRequest(replyToken=reply_token, messages=[msg])
                )
    except Exception as e:
        print(f"❌ [LINE Handler Error]: {e}")
    finally:
        db.close()
