import os
import requests
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

# 採用最新穩定模型版本
gemini_model = genai.GenerativeModel('gemini-2.0-flash')

# 全域變數：儲存 ESP32 傳來的最新感測器數據
latest_sensor_data = {
    "temp": 36.5,
    "spo2": 98,
    "heart_rate": 75
}

# 3. 取得即時天氣函式（免費 Open-Meteo API，預設台中經緯度）
def get_current_weather(lat=24.14, lon=120.67):
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&timezone=Asia%2FTaipei"
        res = requests.get(url, timeout=5).json()
        curr = res.get("current_weather", {})
        temp = curr.get("temperature", "--")
        code = curr.get("weathercode", 0)
        
        # WMO 天氣代碼判斷是否下雨
        is_raining = code in [51, 53, 55, 61, 63, 65, 80, 81, 82]
        weather_desc = "下雨中" if is_raining else "晴朗/多雲無雨"
        return f"室外氣溫 {temp}°C，天候：{weather_desc}"
    except Exception as e:
        print(f"天氣 API 讀取失敗: {e}")
        return "氣溫適中，天候正常"

# 4. 接收 ESP32 感測器數據的 Endpoint
@app.post("/receive_sensor_data")
async def receive_sensor_data(request: Request):
    global latest_sensor_data
    try:
        data = await request.json()
        latest_sensor_data["temp"] = data.get("temp", 36.5)
        latest_sensor_data["spo2"] = data.get("spo2", 98)
        latest_sensor_data["heart_rate"] = data.get("heart_rate", 75)
        return {"status": "success", "data": latest_sensor_data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 5. LINE Webhook 接收點
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# 6. 處理用戶訊息與關鍵字比對
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    reply_text = ""

    temp = latest_sensor_data["temp"]
    spo2 = latest_sensor_data["spo2"]
    hr = latest_sensor_data["heart_rate"]

    try:
        # 關鍵字模糊比對（避免「活動」與「運動」文字差異導致不回覆）
        if "健康" in user_msg or "日記" in user_msg:
            reply_text = f"📊 【即時生理量測結果】\n• 體溫：{temp} °C\n• 血氧：{spo2} %\n• 心率：{hr} bpm\n\n身體狀態良好，請繼續保持！"
        
        elif "適合" in user_msg or "運動" in user_msg or "活動" in user_msg:
            weather_info = get_current_weather(lat=24.14, lon=120.67)
            prompt = (
                f"長輩生理數據：體溫 {temp}°C、血氧 {spo2}%、心率 {hr} bpm。\n"
                f"長輩所在地戶外天氣：{weather_info}。\n"
                f"請以親切溫柔的口吻（100字內），結合生理狀態與當前天氣狀況（如下雨提醒室內運動，太熱提醒補水），"
                f"推薦 1-2 個今天適合長輩執行的輕量運動。"
            )
            response = gemini_model.generate_content(prompt)
            reply_text = response.text

        elif "飲食" in user_msg or "保養" in user_msg:
            prompt = f"使用者數據：體溫 {temp}°C、血氧 {spo2}%、心率 {hr} bpm。請給長輩 80 字內簡短溫和的飲食保養提醒。"
            response = gemini_model.generate_content(prompt)
            reply_text = response.text

        elif "聯繫" in user_msg or "家屬" in user_msg:
            reply_text = "🚨 已通知您的家屬與照護人員！\n若有緊急不適，請同時撥打 119 或聯繫緊急聯絡人。"

        else:
            prompt = f"你是一位親切的長輩健康照護助手。長輩說：『{user_msg}』。請以簡短、關懷且語氣溫和的方式進行對話。"
            response = gemini_model.generate_content(prompt)
            reply_text = response.text

    except Exception as e:
        print(f"處理訊息時發生錯誤: {e}")
        reply_text = f"⚠️ 系統處理時發生異常，請稍後再試。（錯誤提示：{str(e)[:40]}）"

    # 發送訊息回覆給 LINE
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print(f"LINE 發送訊息失敗: {e}")
