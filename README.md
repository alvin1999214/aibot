# Instagram 群組聊天 Bot

透過 Docker Compose 啟動，透過主機 IP 登入專用 Instagram 帳號。把該帳號加入群組後，群友傳送 `@你的bot帳號 問題`，Bot 會把同群組近期文字上下文交給 `.env` 指定的模型並回覆。

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

開啟 `http://主機IP:8001`（主機本機也可用 http://localhost:8001），以 `admin` 和 `ADMIN_PASSWORD` 通過瀏覽器管理頁登入，再填入 **Bot 的 Instagram 帳密**。Session 會自動取得並保存在 Docker volume，Bot 隨即啟動。密碼不寫入磁碟；token 不回傳前端。

- 若出現雙重驗證要求，重新填入帳密及當下 2FA 驗證碼後送出。
- 若 Instagram 發送 Email／SMS challenge，頁面會出現驗證碼表單，請於 5 分鐘內提交。
- 部分 challenge 必須到官方 Instagram App 完成，之後再試登入；不會繞過平台驗證。
- 也可展開替代登入，輸入自己已登入 Instagram 瀏覽器 Cookie 中的 `sessionid`。Instagram 可能拒絕 Web session；帳密登入產生的完整 session 通常更合適。
- 把 Bot 加入群組。若邀請進入訊息要求，先用 Instagram App 接受，再於群組輸入 `@帳號 你好`。

## 行為與設定

只回覆群組中的文字 `@username`，大小寫不敏感，不回覆自己的訊息或私訊。媒體內容與語音目前不送入模型。每個群組及帳號的上下文獨立保存，包括未提及 Bot 的近期文字。請讓群組成員知悉這些文字會傳至你設定的 API 服務。

| 變數 | 預設值 | 用途 |
| --- | --- | --- |
| `PORT` | `8001` | 主機對外管理頁埠 |
| `ALLOWED_HOSTS` | `*` | 允許的主機 IP／域名，逗號分隔、不含埠；限制時請加入 `127.0.0.1` 供健康檢查使用 |
| `POLL_SECONDS` | `20` | 輪詢間隔，最低 10 秒；錯誤時退避至最多 300 秒 |
| `CONTEXT_MESSAGES` | `40` | 每個群組讀取及模型使用的近期訊息數，最高 200 |
| `CONTEXT_CHARS` | `16000` | 文字內容字元上限，不是 token 上限 |
| `THREAD_LIMIT` | `50` | 每轮讀取最近的對話數，包含私訊；只處理其中群組 |
| `SYSTEM_PROMPT` | 繁體中文助理 | 模型的 system prompt |
| `IG_PROXY` | 空 | 選用固定出口代理 URL |

現有 `.env.example` 的 `IMAGE_*` 設定保留但此文字 Bot 不使用。修改 `.env` 後執行 `docker compose up -d --force-recreate`。

首次成功登入之前的舊訊息只作為上下文。重啟會重用 session 和去重紀錄，並處理仍落在輪詢視窗內的新提及。高流量群組、停機太久或超過 `THREAD_LIMIT` 的對話可能漏讀；此版本為有限視窗輪詢，不是全歷史同步。回覆最長 900 字元。

送出回覆前會先記錄 claim，避免重啟或網路逾時導致重複發送。若送出結果不明，該則不自動重送，使用者可再次 @ Bot。模型請求失敗則會退避重試。健康檢查只代表管理服務在線；Instagram／模型錯誤請看管理頁狀態。

## 資料與維護

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

測試使用假的 Instagram／模型服務，涵蓋提及比對、上下文隔離、首次登入基準、重複回覆防護、錯誤重試、API 格式及管理頁驗證。實際 Instagram 登入、challenge 與訊息收發需要你自己的帳號及 API 設定才能驗證。
