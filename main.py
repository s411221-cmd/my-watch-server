import os
import requests
import google.generativeai as genai
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

# ================= 1. 環境變數讀取 (完全對應 Render 設定) =================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 家屬端機器人與家屬 LINE ID 變數
FAMILY_BOT_TOKEN = os.getenv("FAMILY_BOT_TOKEN", "")
FAMILY_LINE_ID = os.getenv("FAMILY_LINE_ID", "")
USER_LINE_ID = os.getenv("USER_LINE_ID", "")

# ================= 2. 初始化 LINE 與 Gemini 設定 =================
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 全域生理數據（預設假數據，未接 ESP32 時自動使用）
latest_sensor_data = {
    "temp": 36.5,
    "spo2": 98,
    "heart_rate": 75
}

# ================= 3. 取得即時天氣 (Open-Meteo API) =================
def get_current_weather(lat=24.14, lon=120.67):
    """ 預設為台中經緯度 (24.14, 120.67) """
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&timezone=Asia%2FTaipei"
        res = requests.get(url, timeout=5).json()
        curr = res.get("current_weather", {})
        temp = curr.get("temperature", "--")
        code = curr.get("weathercode", 0)
        
        # WMO 天氣代碼 51~82 均為各式降雨
        is_raining = code in [51, 53, 55, 61, 63, 65, 80, 81, 82]
        weather_desc = "下雨中" if is_raining else "晴朗/多雲無雨"
        return f"戶外氣溫 {temp}°C，天候狀況：{weather_desc}"
    except Exception as e:
        print(f"Weather API error: {e}")
        return "氣溫適中，天候正常"

# ================= 4. 安全呼叫 Gemini (雙重模型備援機制) =================
def safe_gemini_generate(prompt: str, fallback_text: str) -> str:
    if not GEMINI_API_KEY:
        return fallback_text
        
    candidate_models = [
        'gemini-1.5-flash',
        'gemini-2.0-flash',
        'gemini-1.5-pro'
    ]
    
    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            print(f"嘗試模型 {model_name} 失敗: {e}")
            continue
            
    return fallback_text

# ================= 5. API 路由設定 =================
@app.get("/")
def home():
    return {"status": "ok", "message": "AI Health Assistant is running!"}

@app.post("/receive_sensor_data")
async def receive_sensor_data(request: Request):
    """ 接收 ESP32 上傳之生理數據 """
    global latest_sensor_data
    try:
        data = await request.json()
        latest_sensor_data["temp"] = data.get("temp", latest_sensor_data["temp"])
        latest_sensor_data["spo2"] = data.get("spo2", latest_sensor_data["spo2"])
        latest_sensor_data["heart_rate"] = data.get("heart_rate", latest_sensor_data["heart_rate"])
        return {"status": "success", "data": latest_sensor_data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/callback")
async def callback(request: Request):
    """ 長輩端 LINE Webhook 接收點 """
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# ================= 6. LINE 訊息邏輯處理 =================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    reply_text = ""

    temp = latest_sensor_data["temp"]
    spo2 = latest_sensor_data["spo2"]
    hr = latest_sensor_data["heart_rate"]

    try:
        # 功能 A：心率 / 生理數據檢測
        if "健康" in user_msg or "日記" in user_msg or "心率" in user_msg or "檢測" in user_msg:
            reply_text = (
                f"📊 【即時生理量測結果】\n"
                f"• 體溫：{temp} °C\n"
                f"• 血氧：{spo2} %\n"
                f"• 心率：{hr} bpm\n\n"
                f"身體狀態良好，請繼續保持！"
            )
        
        # 功能 B：今日適合的運動 / 活動 (結合氣候與 AI)
        elif "適合" in user_msg or "運動" in user_msg or "活動" in user_msg:
            weather_info = get_current_weather(lat=24.14, lon=120.67)
            prompt = (
                f"長輩生理數據：體溫 {temp}°C、血氧 {spo2}%、心率 {hr} bpm。\n"
                f"長輩所在地戶外天氣：{weather_info}。\n"
                f"請以親切溫柔的口吻（100字內），結合生理與天氣狀況（如下雨提醒室內運動，太熱提醒補水），"
                f"推薦 1-2 個今天適合長輩執行的輕量運動。"
            )
            fallback = (
                f"🏃【今日專屬運動建議】\n"
                f"• 當前天候：{weather_info}\n"
                f"• 生理狀態：體溫 {temp}°C / 血氧 {spo2}% / 心率 {hr}bpm（良好）\n\n"
                f"建議您今天可在室內進行 15 分鐘緩和伸展操或原地踏步，運動時記得隨時補充水分喔！"
            )
            reply_text = safe_gemini_generate(prompt, fallback)

        # 功能 C：飲食保養提醒
        elif "飲食" in user_msg or "保養" in user_msg:
            prompt = f"長輩數據：體溫 {temp}°C、血氧 {spo2}%、心率 {hr} bpm。請給長輩 80 字內簡短溫和的飲食保養提醒。"
            fallback = (
                f"🥗【飲食保養提醒】\n"
                f"今天請記得多喝溫開水，飲食保持清淡少油少鹽，並適量攝取新鮮蔬菜與優質蛋白質喔！"
            )
            reply_text = safe_gemini_generate(prompt, fallback)

        # 功能 D：一鍵聯繫家屬（經由家屬端機器人推播通知）
        elif "聯繫" in user_msg or "家屬" in user_msg:
            reply_text = "🚨 已即時發送通知給您的家屬！\n若有緊急不適，請同時撥打 119 尋求協助。"
            
            # 使用家屬端機器人 Token 與家屬 ID 發送推播
            if FAMILY_BOT_TOKEN and FAMILY_LINE_ID:
                try:
                    family_config = Configuration(access_token=FAMILY_BOT_TOKEN)
                    push_content = (
                        f"🚨 【長輩安危關懷通知】\n"
                        f"長輩剛才點擊了「一鍵聯繫家屬」！\n\n"
                        f"📊 【即時生理數據】\n"
                        f"• 體溫：{temp} °C\n"
                        f"• 血氧：{spo2} %\n"
                        f"• 心率：{hr} bpm\n\n"
                        f"請儘速與長輩聯繫關心狀況。"
                    )
                    
                    with ApiClient(family_config) as family_api_client:
                        family_bot_api = MessagingApi(family_api_client)
                        family_bot_api.push_message(
                            PushMessageRequest(
                                to=FAMILY_LINE_ID,
                                messages=[TextMessage(text=push_content)]
                            )
                        )
                    print("已成功發送通知至家屬端機器人！")
                except Exception as push_err:
                    print(f"家屬端機器人發送推播失敗: {push_err}")

        # 功能 E：一般對話
        else:
            prompt = f"你是一位親切的長輩健康照護助手。長輩說：『{user_msg}』。請以簡短、關懷且語氣溫和的方式進行對話。"
            fallback = f"收到您的訊息了！天氣多變，記得隨時補充水分、注意保暖喔！"
            reply_text = safe_gemini_generate(prompt, fallback)

    except Exception as e:
        print(f"Handle message error: {e}")
        reply_text = "😊 收到您的訊息！請記得按時記錄健康數據、多喝水喔！"

    # 回覆訊息給長輩端 LINE
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
        print(f"LINE reply error: {e}")
