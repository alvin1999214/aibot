# Instagram 群組聊天 Bot

透過 Docker Compose 啟動，透過主機 IP 開啟管理頁並匯入專用 Instagram 帳號的 sessionid。把該帳號加入群組後，群友傳送 `@你的bot帳號 問題` 或直接回覆 Bot 發送的訊息，Bot 會把同群組近期文字上下文交給 `.env` 指定的模型並回覆。

Instagram 官方 Messaging API [不支援群組聊天](https://www.postman.com/meta/instagram/folder/uxudqu0/send-api)。本專案使用 [instagrapi](https://github.com/subzeroid/instagrapi) 非官方 API，不能保證 Instagram 接受每次登入或長期可用；可能遇到安全驗證、限流或帳號限制。這裡使用的是帳號 session，並非官方 OAuth access token。

## 啟動

需要 Docker Engine 和 Docker Compose v2。

```bash
cp .env.example .env
# 編輯 .env 後啟動
docker compose up -d --build
```

必填設定：

| 變數 | 用途 |
| --- | --- |
| `API_KEY` | 模型服務 API key |
| `BASE_URL` | 相容 Chat Completions 的 API 根網址，例如 `https://your-provider.example/v1`；程式會接上 `/chat/completions` |
| `MODEL` | 服務提供的模型名稱 |
| `ADMIN_PASSWORD` | 自訂至少 16 字元的管理密碼，必須更換範例值 |

開啟 `http://主機IP:8001`（主機本機也可用 http://localhost:8001），以 `admin` 和 `ADMIN_PASSWORD` 通過管理頁驗證。頁面只接受 **sessionid**，沒有 Instagram 帳密、2FA 驗證碼或推播核准登入功能。

### 取得 sessionid

1. 在你自己的電腦上，用 Chrome、Edge 或 Firefox 開啟 https://www.instagram.com/，登入 Bot 帳號。
2. 在 Instagram 完成全部驗證（包含手機推播核准），確認瀏覽器已進入該帳號。
3. 在 Instagram 分頁按 F12，或從瀏覽器選單開啟開發者工具。
4. Chrome／Edge：**Application → Storage → Cookies → https://www.instagram.com**；Firefox：**Storage → Cookies → https://www.instagram.com**。
5. 找到 `sessionid`，複製 **Value**；只複製值，不要包含 `sessionid=`、引號或其他 Cookie。
6. 貼入 Bot 管理頁，按「匯入 Session 並啟動 Bot」，確認狀態為「運行中」及帳號正確。

Bot 主機不需要 GUI。驗證成功後完整 Session 會保存在 Docker volume，重啟時重用，不回傳至前端。既有有效的 Session 也會繼續使用。

若找不到 Cookie，請確認瀏覽器已登入，重新整理 Instagram 分頁後再查看。若匯入或恢復失敗，請在 Instagram 官方網站完成登入與驗證，再取得新的 sessionid 匯入；Instagram 可能拒絕跨主機使用 Web session。本服務不會要求輸入 Instagram 密碼、驗證碼或在終端互動登入。

把 Bot 加入群組。若邀請進入訊息要求，先用 Instagram App 接受，再於群組輸入 `@帳號 你好`。

更新既有部署：

```bash
docker compose up -d --build --force-recreate
```

## 行為與設定

群組中的文字訊息只要包含 `@username`（大小寫不敏感），或使用 Instagram 的回覆功能回覆 Bot 發送的訊息，就會觸發回覆。不回覆自己的訊息或私訊。媒體內容與語音目前不送入模型。每個群組及帳號的上下文獨立保存，包括未提及 Bot 的近期文字。請讓群組成員知悉這些文字會傳至你設定的 API 服務。

| 變數 | 預設值 | 用途 |
| --- | --- | --- |
| `PORT` | `8001` | 主機對外管理頁埠 |
| `ALLOWED_HOSTS` | `*` | 允許的主機 IP／域名，逗號分隔、不含埠；限制時請加入 `127.0.0.1` 供健康檢查使用 |
| `POLL_SECONDS` | `20` | 輪詢間隔，最低 10 秒；錯誤時退避至最多 300 秒 |
| `CONTEXT_MESSAGES` | `40` | 每個群組讀取及模型使用的近期訊息數，最高 200 |
| `CONTEXT_CHARS` | `16000` | 文字內容字元上限，不是 token 上限 |
| `THREAD_LIMIT` | `50` | 每轮讀取最近的對話數，包含私訊；只處理其中群組 |
| `SYSTEM_PROMPT` | 繁體中文助理 | 模型的 system prompt |
| `IMAGE_MODEL` | 未設定時關閉 | 圖片模型名稱；沿用 `BASE_URL`／`API_KEY` 的 `/chat/completions` |
| `IMAGE_ASPECT_RATIO` | `1:1` | 圖片比例，傳至 `image_config.aspect_ratio` |
| `IMAGE_SIZE` | `1K` | 生成解析度，傳至 `image_config.image_size`；可用值依圖片服務而定 |
| `IG_PROXY` | 空 | 選用固定出口代理 URL |

修改 `.env` 後執行 `docker compose up -d --force-recreate`。

### 生成圖片

設定 `IMAGE_MODEL` 後，在群組輸入 `@你的bot帳號 畫一隻穿太空衣的貓`，或直接回覆 Bot 的訊息提出畫圖要求。文字模型 `MODEL` 會根據對話決定是否呼叫 `generate_image`，整理完整提示詞，再交給 `IMAGE_MODEL` 生成一張圖片並發送到原群組。一般聊天維持文字回覆。

`MODEL` 必須支援 Chat Completions 的 `tools`／`tool_calls`。圖片請求使用相同的 `/chat/completions`，帶入 `modalities: ["image", "text"]` 和 `image_config`（[介面格式參考](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion)）；服務需支援這些欄位及指定的圖片模型。支援 `choices[0].message.images[].image_url.url`、`content` 中的 `image_url` 區塊，或 Markdown 內的 `data:image/...;base64,...`。目前不下載外部圖片 URL，也不支援上傳圖片的辨識或編輯；若回覆先前生成的圖片要求變化，會依保存的文字提示詞重新生成。

圖片經驗證後轉成 JPEG，最長邊縮至 1080 像素，使用 Instagram 圖片訊息發送；發送結束後清除暫存檔。原始圖片限制為 20 MiB、2500 萬像素。上下文保存圖片提示詞，logs 記錄 `media_type=image`、提示詞及發送狀態，不記錄 base64 內容。生成失敗會沿用模型錯誤退避重試；發送結果不明則不自動重送。圖片生成每次呼叫可能產生服務費用，包括失敗後重新生成。

首次成功登入之前的舊訊息只作為上下文。重啟會重用 session 和去重紀錄，並處理仍落在輪詢視窗內的新提及或回覆 Bot 的文字訊息。高流量群組、停機太久或超過 `THREAD_LIMIT` 的對話可能漏讀；此版本為有限視窗輪詢，不是全歷史同步。回覆最長 900 字元。

送出回覆前會先記錄 claim，避免重啟或網路逾時導致重複發送。若送出結果不明，該則不自動重送，使用者可再次 @ Bot。模型請求失敗則會退避重試。健康檢查只代表管理服務在線；Instagram／模型錯誤請看管理頁狀態。

## 資料與維護

使用 `docker compose logs -f --tail=100 instagram-bot` 查看對話輸入與輸出。`conversation.input` 包含觸發回覆的文字、發送者及送給模型的群組上下文；`conversation.output` 包含實際準備發送的回覆，`status=sent` 表示發送成功，`status=uncertain` 表示發送結果不明。模型失敗會記錄 `conversation.model_failed`。每筆紀錄帶有時間、帳號、群組 ID 和觸發訊息 ID，方便配對追蹤；已處理的提及不會因輪詢重複記錄，模型失敗重試則會再次記錄輸入。對話 logs 包含聊天原文，請限制存取；不會記錄 API key 或 Session 設定。

`bot-data` volume 保存 `/data/session.json`、SQLite 上下文和處理紀錄。Session 等同登入憑證，請保護備份。每群文字保留最多 `CONTEXT_MESSAGES × 3` 筆（成功處理後清理），去重 ID 長期保留。管理頁綁定 `0.0.0.0`，可從其他電腦透過主機 IP 存取，並保留管理密碼與跨站請求防護。主機防火牆需允許管理電腦連入 TCP `8001`（或自訂 `PORT`）。HTTP 不加密登入資料，請在可信任內網使用；若跨網際網路，請搭配 HTTPS。

```bash
docker compose logs -f --tail=100
docker compose down      # 保留登入及資料
# 完全刪除登入與聊天資料（不可復原）：
docker compose down -v
```

## 本機測試

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

測試使用假的 Instagram／模型服務，涵蓋提及與回覆觸發、上下文隔離、首次登入基準、重複回覆防護、錯誤重試、API 格式、圖片工具呼叫與解碼、圖片發送及暫存清理，以及管理頁驗證。實際 Instagram Session 匯入、模型生圖與訊息收發需要你自己的帳號及 API 設定才能驗證。
