import os
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

# 1. 環境變數讀取
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. 初始化 LINE 與 Gemini
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

# 全域變數：儲存 ESP32 傳來的最新感測器數據
latest_sensor_data = {
    "temp": 36.5,
    "spo2": 98,
    "heart_rate": 75
}

# 3. 接收 ESP32 感測器數據的 Endpoint
@app.post("/receive_sensor_data")
async def receive_sensor_data(request: Request):
    global latest_sensor_data
    data = await request.json()
    latest_sensor_data["temp"] = data.get("temp", 36.5)
    latest_sensor_data["spo2"] = data.get("spo2", 98)
    latest_sensor_data["heart_rate"] = data.get("heart_rate", 75)
    return {"status": "success", "data": latest_sensor_data}

# 4. LINE Webhook 接收點
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# 5. 處理用戶訊息與關鍵字比對
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    reply_text = ""

    # 讀取當前最新的健康數據
    temp = latest_sensor_data["temp"]
    spo2 = latest_sensor_data["spo2"]
    hr = latest_sensor_data["heart_rate"]

    # 關鍵字判斷邏輯
    if user_msg == "本週健康日記":
        reply_text = f"📊 【即時生理量測結果】\n• 體溫：{temp} °C\n• 血氧：{spo2} %\n• 心率：{hr} bpm\n\n身體狀態良好，請繼續保持！"
    
    elif user_msg == "今日適合的運動":
        prompt = f"使用者目前的體溫為 {temp}°C，血氧 {spo2}%，心率 {hr} bpm。請用繁體中文、溫柔且適合長輩的口吻，推薦 1-2 個今天適合做的輕度運動或伸展操，字數在 100 字以內。"
        response = gemini_model.generate_content(prompt)
        reply_text = response.text
        
    elif user_msg == "飲食保養建議":
        prompt = f"使用者當前健康數據：體溫 {temp}°C、血氧 {spo2}%、心率 {hr} bpm。請用溫和簡短的口吻（80字內），給長輩今日飲食上的保健提醒或食材建議。"
        response = gemini_model.generate_content(prompt)
        reply_text = response.text

    elif user_msg == "一鍵聯繫家屬":
        reply_text = "🚨 已通知您的家屬與照護人員！\n若有緊急不適，請同時撥打 119 或聯繫緊急聯絡人。"

    else:
        # 其他文字訊息由 Gemini 自由回答
        prompt = f"你是一位親切的長輩健康照護助手。長輩說：『{user_msg}』。請以簡短、關懷且語氣溫和的方式進行對話。"
        response = gemini_model.generate_content(prompt)
        reply_text = response.text

    # 回覆訊息給 LINE 用戶
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
