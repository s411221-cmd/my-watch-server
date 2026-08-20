import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from linebot import LineBotApi
from linebot.models import TextSendMessage
from google import genai

app = FastAPI(title="AI 智慧健康手錶伺服器（雙機器人版）")

# ==================== 1. 設定與金鑰（雙機器人） ====================
USER_BOT_TOKEN = os.getenv("USER_BOT_TOKEN", "")      # 配戴者機器人 Token
FAMILY_BOT_TOKEN = os.getenv("FAMILY_BOT_TOKEN", "")  # 家屬機器人 Token

USER_LINE_ID = os.getenv("USER_LINE_ID", "")          # 配戴者 LINE ID
FAMILY_LINE_ID = os.getenv("FAMILY_LINE_ID", "")      # 家屬 LINE ID

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 初始化兩隻 LINE 機器人 SDK 與 Gemini Client
user_line_bot = LineBotApi(USER_BOT_TOKEN) if USER_BOT_TOKEN else None
family_line_bot = LineBotApi(FAMILY_BOT_TOKEN) if FAMILY_BOT_TOKEN else None
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==================== 2. 手錶傳輸資料格式 ====================
class SensorData(BaseModel):
    watch_id: str     # 手錶編號
    temp: float       # 體溫
    spo2: int         # 血氧
    heart_rate: int   # 心率

# ==================== 3. 用戶資料庫 ====================
USER_DATABASE = {
    "Watch_001": {
        "user_line_id": USER_LINE_ID,
        "family_line_id": FAMILY_LINE_ID
    }
}

# ==================== 4. AI 生成建議函式 ====================
def fallback_advice(status: str) -> str:
    if status == "綠燈":
        return "【AI 健康提醒】今日生理數據非常優秀！請繼續保持愉快心情並多補充水分喔！"
    elif status == "黃燈":
        return "【AI 健康提醒】體溫或血氧稍微波動，請先坐下休息、喝杯溫水放鬆一下。"
    else:
        return "🚨【AI 緊急警告】數據顯示嚴重異常，請立刻坐正深呼吸，已連線緊急通知家屬！"

def generate_ai_advice(temp: float, spo2: int, heart_rate: int, status: str) -> str:
    if not ai_client:
        return fallback_advice(status)

    prompt = f"""
你是一位專業、親切且有同理心的銀髮族家庭醫師助手。
目前手錶量測到的長輩生理數據如下：
- 燈號狀態：{status}
- 體溫：{temp} °C
- 血氧：{spo2} %
- 心率：{heart_rate} 次/分

請根據以上數據與燈號，為長輩撰寫一段溫馨且具體的健康建議與提醒。
要求：
1. 語氣要親切溫柔，適合長輩閱讀，繁體中文。
2. 若為「綠燈」：給予讚美與日常保健小叮嚀（例如多喝水、適度散步），字數約 50~80 字。
3. 若為「黃燈」：表達關心並給予具體緩和措施（例如坐下休息、補充溫水、保持通風），字數約 60~90 字。
4. 若為「紅燈」：語氣要嚴謹但不過度恐慌，給予即時安撫與深呼吸指導，並告知已連線通知家屬，字數約 60~90 字。
5. 不要輸出任何 Markdown 標題（如 ## 或 **），直接輸出文字即可。
"""
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"AI 生成失敗：{e}")
        return fallback_advice(status)

# ==================== 5. 首頁測試用 API ====================
@app.get("/")
async def root():
    return {"status": "online", "message": "雙機器人 AI 健康手錶伺服器正常運作中！"}

# ==================== 6. 接收手錶數據 API ====================
@app.post("/receive_sensor_data")
async def receive_sensor_data(data: SensorData):
    watch_id = data.watch_id
    temp = data.temp
    spo2 = data.spo2
    hr = data.heart_rate

    if watch_id not in USER_DATABASE:
        raise HTTPException(status_code=404, detail="找不到此手錶 ID")

    target = USER_DATABASE[watch_id]
    
    # 判斷燈號
    status = "綠燈"
    if temp > 38.0 or spo2 < 90:
        status = "紅燈"
    elif temp > 37.3 or spo2 < 95:
        status = "黃燈"

    # 🤖 呼叫 Gemini AI 自動生成專屬叮嚀
    ai_advice = generate_ai_advice(temp, spo2, hr, status)

    try:
        # 1. 透過【配戴者機器人】傳送給長輩本人
        if user_line_bot and target["user_line_id"]:
            user_msg = f"【AI 醫師叮嚀】\n{ai_advice}\n\n(📊 即時數據：體溫 {temp}°C / 血氧 {spo2}% / 心率 {hr}bpm)"
            user_line_bot.push_message(target["user_line_id"], TextSendMessage(text=user_msg))

        # 2. 若數據異常（黃燈或紅燈），透過【家屬機器人】發送報告給家屬
        if status in ["黃燈", "紅燈"] and family_line_bot and target["family_line_id"]:
            family_msg = f"🚨【長輩健康警訊通知】\n手錶 [{watch_id}] 傳回異常數據！\n\n- 狀態：{status}\n- 體溫：{temp}°C\n- 血氧：{spo2}%\n- 心率：{hr}bpm\n\n🤖 AI 醫師分析：\n{ai_advice}\n\n請盡快確認長輩狀況！"
            family_line_bot.push_message(target["family_line_id"], TextSendMessage(text=family_msg))

        return {
            "status": "success",
            "light_status": status,
            "ai_advice": ai_advice
        }

    except Exception as e:
        print(f"LINE 發送失敗：{e}")
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
