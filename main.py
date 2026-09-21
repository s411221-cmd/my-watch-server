import os
import json
import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    TextMessage, FlexMessage, FlexContainer, QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import google.generativeai as genai

# ===== 1. 環境變數設定 =====
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# 全局變數保存最新生理數據
latest_telemetry = {"temp": 36.5, "heart_rate": 75, "spo2": 98}

# ===== 2. 背景 Keep-Alive 任務 =====
async def keep_alive_task():
    """每 10 分鐘自動對自己發送 GET 請求，防止 Render 免費版進入休眠"""
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try:
                res = await client.get(f"{RENDER_URL}/api/v1/healthz")
                print(f"[Keep-Alive] Ping 心跳成功, Status: {res.status_code}")
            except Exception as e:
                print(f"[Keep-Alive] Ping 失敗: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(keep_alive_task())
    yield

app = FastAPI(lifespan=lifespan)

# ===== 3. API Endpoints =====
@app.get("/api/v1/healthz")
async def health_check():
    return {"status": "healthy", "service": "health-watch-backend"}

@app.post("/api/v1/telemetry")
async def receive_telemetry(data: dict):
    global latest_telemetry
    latest_telemetry["temp"] = data.get("temp", 36.5)
    latest_telemetry["heart_rate"] = data.get("heart_rate", 75)
    latest_telemetry["spo2"] = data.get("spo2", 98)
    return {"status": "success", "data_received": latest_telemetry}

# ===== 4. AI 飲食與運動建議產生器 =====
def generate_ai_advice(category: str, symptom: str = "無症狀") -> str:
    temp = latest_telemetry["temp"]
    hr = latest_telemetry["heart_rate"]
    
    prompt = f"""
    你是一位溫柔專業的家庭醫師與營養師。請根據使用者當前數據與需求，給予溫馨且具體的保養建議：
    【類別】: {category}
    【即時數據】: 體溫 {temp} °C, 心率 {hr} bpm
    【主訴狀況】: {symptom}
    
    【要求】:
    1. 針對數據與症狀給予 2-3 點具體建議（若體溫 > 37.5°C 須提醒補充水分與休息）。
    2. 語氣體貼親切，控制在 120 字以內。
    """
    response = gemini_model.generate_content(prompt)
    return response.text.strip()

def build_flex_card(title: str, advice: str) -> FlexContainer:
    temp = latest_telemetry["temp"]
    hr = latest_telemetry["heart_rate"]
    bg_color = "#2E7D32" if "飲食" in title else "#E65100"
    
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

# ===== 5. LINE Bot Webhook 處理器 =====
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

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)

        # A. 點擊「今日適合的運動」
        if text == "今日適合的運動":
            advice = generate_ai_advice("運動建議")
            card = build_flex_card("🏃 今日專屬運動建議", advice)
            messaging_api.reply_message(ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="運動建議", contents=card)]))

        # B. 點擊「飲食保養建議」➔ 觸發 Quick Reply 詢問症狀
        elif text == "飲食保養建議":
            qr = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label="😊 無症狀/狀況良好", text="飲食調查:無症狀")),
                QuickReplyItem(action=MessageAction(label="🤢 胃口差/腸胃不適", text="飲食調查:腸胃不適")),
                QuickReplyItem(action=MessageAction(label="😴 頭暈/疲勞倦怠", text="飲食調查:頭暈疲勞")),
                QuickReplyItem(action=MessageAction(label="🤒 喉嚨痛/發燒感", text="飲食調查:發燒不適"))
            ])
            msg = TextMessage(text="請選擇您目前是否有任何身體狀況，AI 營養師將為您評估最合適的飲食：", quick_reply=qr)
            messaging_api.reply_message(ReplyMessageRequest(replyToken=reply_token, messages=[msg]))

        # C. 接收 Quick Reply 的症狀選項並產出客製化卡片
        elif text.startswith("飲食調查:"):
            symptom = text.replace("飲食調查:", "")
            advice = generate_ai_advice("飲食保養", symptom)
            card = build_flex_card("🥗 今日專屬飲食建議", f"【狀況：{symptom}】\n{advice}")
            messaging_api.reply_message(ReplyMessageRequest(replyToken=reply_token, messages=[FlexMessage(altText="飲食建議", contents=card)]))
