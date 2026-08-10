from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from linebot import LineBotApi
from linebot.models import TextSendMessage
import random
import uvicorn

# 初始化 FastAPI 應用
app = FastAPI(title="智慧手錶健康伺服器")

# ==================== 1. 設定與金鑰 ====================
# 請替換成你在 LINE Developers 後台申請到的 Channel Access Token
LINE_CHANNEL_ACCESS_TOKEN = "你的_LINE_CHANNEL_ACCESS_TOKEN"
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

# ==================== 2. 定義手錶傳過來的 JSON 格式 ====================
class SensorData(BaseModel):
    watch_id: str     # 手錶編號 (例如: "Watch_001")
    temp: float       # 體溫 (例如: 38.2)
    spo2: int         # 血氧 (例如: 91)
    heart_rate: int   # 心率 (例如: 85)

# ==================== 3. 資料庫：對照長輩與家屬的 LINE ID ====================
# 在正式展示時，把對應的 LINE ID 填入此處
USER_DATABASE = {
    "Watch_001": {
        "user_line_id": "長輩的_LINE_USER_ID",   # 長輩的 LINE ID
        "family_line_id": "家屬的_LINE_USER_ID"  # 家屬的 LINE ID
    }
}

# ==================== 4. 方案 B 專家措施庫 ====================
MEASURES_DATABASE = {
    "綠燈": [
        "【健康報報】今日數據非常優秀，體溫與血氧都很正常喔！請繼續保持！",
        "【健康報報】身體狀況良好，今天也別忘了適當補充水分喔！"
    ],
    "黃燈": [
        "【健康提醒】體溫稍微偏高或血氧有些波動。請先坐下休息、喝杯溫水、放鬆心情喔！",
        "【健康提醒】檢測到數據輕微異常，請保持室內空氣流通，稍後會再次為您記錄。"
    ],
    "紅燈": [
        "🚨【緊急警告】系統偵測到您的生理數據發生嚴重異常（體溫過高或血氧過低）！請立刻坐正深呼吸，已同步緊急通知家屬！"
    ]
}

# ==================== 5. 接收手錶數據的 API 網址 ====================
@app.post("/receive_sensor_data")
async def receive_sensor_data(data: SensorData):
    watch_id = data.watch_id
    temp = data.temp
    spo2 = data.spo2
    hr = data.heart_rate

    print(f"收到手錶 [{watch_id}] 數據 ➔ 體溫: {temp}°C | 血氧: {spo2}% | 心率: {hr}BPM")

    # 檢查是否為註冊過的手錶
    if watch_id not in USER_DATABASE:
        raise HTTPException(status_code=404, detail="找不到此手錶 ID")

    target_users = USER_DATABASE[watch_id]
    user_id = target_users["user_line_id"]
    family_id = target_users["family_line_id"]

    # --- 方案 B：自製判斷邏輯 ---
    status = "綠燈"
    if temp > 38.0 or spo2 < 90:
        status = "紅燈"
    elif temp > 37.3 or spo2 < 95:
        status = "黃燈"

    # 從措施庫中隨機抽出一句溫馨叮嚀
    advice = random.choice(MEASURES_DATABASE[status])

    try:
        # 1. 無論什麼燈號，先推播通知給「使用者本人」
        user_msg = f"{advice}\n(目前數據：體溫 {temp}°C / 血氧 {spo2}% / 心率 {hr}BPM)"
        line_bot_api.push_message(user_id, TextSendMessage(text=user_msg))

        # 2. 如果亮「紅燈」，額外推播緊急警告給「家屬」
        if status == "紅燈":
            family_msg = f"🚨【家人緊急通知】\n您綁定的長輩手錶 [{watch_id}] 傳回危險警訊！\n即時數據：體溫 {temp}°C / 血氧 {spo2}%\n請立刻確認家人安全！"
            line_bot_api.push_message(family_id, TextSendMessage(text=family_msg))

        return {
            "status": "success",
            "light_status": status,
            "message": "數據已處理，LINE 訊息已發送"
        }

    except Exception as e:
        print(f"LINE 發送失敗：{e}")
        return {"status": "error", "detail": str(e)}

# ==================== 6. 本地啟動伺服器 ====================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)