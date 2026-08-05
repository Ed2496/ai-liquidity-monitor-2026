# 2026 AI 流動性壓力測試指標 — 自動監控儀表板

> **穿境者視角（Channel Fault Line Monitor）**
> 基於五維度加權計分，每日自動抓取 FRED / Yahoo Finance 數據，生成可公開瀏覽的互動儀表板。

---

## 🔧 資料層修正說明（裸投檢查校準 2026-08-05）

本版基於 Kimi 原始部署包。**模型層閾值與權重 100% 保留（DeepSeek 原始計分卡），僅修正會靜默出錯的資料/計分 bug**，並把「這格是真數據還是預設值」焊進儀表板：

| # | 問題（原碼） | 症狀 | 修正 |
|:--|:--|:--|:--|
| FIX-1 | HY 利差 `BAMLH0A0HYM2` | FRED 單位是百分比(約3~3.5)，卻拿去跟 400/600 **bps** 比 → 維度2 **永遠 0 分** | 抓取後 ×100 轉 bps |
| FIX-2 | 維度4 ticker `COR` | CoreSite 2021 底被併購下市，`COR` 現為 **Cencora（藥品通路商）** | 移除，改 `DLR / EQIX / IRM` |
| FIX-3 | REIT 殖利率 `info["dividendYield"]` | yfinance 版本間單位不一，×100 會變 300% | 改 **TTM 實配息 ÷ 現價** 為主路徑 |
| FIX-4 | `divs.last("365D")` | pandas 2.2+ 已移除 → crash | 改 index 布林遮罩 |
| FIX-5 | 維度1「SOFR 跳升 >0.5%」 | 被降格成「SOFR−DGS10 水位差」（命名降格） | 抓 SOFR 序列算**真實變化量** |
| **FIX-6** | **計分刻度** | **DeepSeek 宣告滿分100/閾值60-40-30，但 raw(0/5/10)×小數權重總分上限只有 10 → 紅綠燈永遠碰不到 60，每天都落「進場」區** | **`total = Σ raw×weight×10`，Python+JS 同步** |
| ADD-1 | VIX 未抓 | 決策樹引用 >35 閘門卻沒有資料 | 補抓 `^VIX` 當 context（不計分） |
| ADD-2 | 無資料來源標示 | 分不清真數據 vs 預設值 | 每維度標 **LIVE/FALLBACK/PROXY/OVERRIDE** + 健康面板 |

**⚠️ FLAG（未改，坍縮權在你）：** 維度4 是「買點越強分數越高」，卻被加進「分數越高越該賣」的總分——買訊號(+10)反而把總分推向離場，與它要觸發的「總分<30進場」閘門相衝（莫比烏斯同面）。此為 DeepSeek 原始計分卡結構，已保留，僅在儀表板標紅。**是否把維度4 拆成獨立「機會軸」不進總分，由你決定。**

**首次執行務必裸投檢查**：本機無 `FRED_API_KEY` 或抓不到 Yahoo 時，全部維度會落 FALLBACK 預設值——健康面板會標灰，**那些數字不可當真數據判斷進出場**。

---

## 📋 功能總覽

| 功能 | 說明 |
|:---|:---|
| **自動數據抓取** | 每日台灣時間早上 9 點自動執行 |
| **五維度計分** | 資金成本、利差崩潰、日圓套利、REITs 錯殺、地緣防火牆 |
| **即時狀態燈號** | 自動判定 🚨 離場 / 🟡 觀察 / 🟢 進場 |
| **互動情境分析** | 網頁上可手動調整維度，模擬不同壓力情境 |
| **零成本部署** | 完全使用 GitHub Actions + GitHub Pages，無伺服器費用 |

---

## 🚀 快速部署（5 分鐘完成）

### 步驟 1：申請 FRED API Key（免費）

1. 前往 [FRED 官網](https://fred.stlouisfed.org/)
2. 點擊右上角 **Sign Up** 註冊免費帳號
3. 登入後進入 [API Keys 頁面](https://fred.stlouisfed.org/docs/api/api_key.html)
4. 點擊 **Request API Key**，複製產生的 Key（格式如 `abcdefghijklmnopqrstuvwxyz123456`）
5. **妥善保存**，下一步會用到

### 步驟 2：創建 GitHub 倉庫

1. 登入 [GitHub](https://github.com)
2. 點擊右上角 **+ → New repository**
3. 倉庫名稱填 `ai-liquidity-monitor-2026`（可自訂）
4. 選擇 **Public**（GitHub Pages 免費版需要公開倉庫）
5. 點擊 **Create repository**

### 步驟 3：上傳本專案檔案

**方法一：直接上傳（推薦新手）**

1. 在本機解壓縮 `ai-liquidity-monitor.zip`
2. 進入你的 GitHub 倉庫頁面
3. 點擊 **Add file → Upload files**
4. 將解壓後的所有檔案與資料夾拖曳上傳（包含 `.github/workflows/`）
5. 點擊 **Commit changes**

**方法二：Git 指令（進階）**

```bash
git clone https://github.com/你的帳號/ai-liquidity-monitor-2026.git
cd ai-liquidity-monitor-2026
# 將解壓後的檔案複製到此資料夾
git add .
git commit -m "Initial commit: AI Liquidity Monitor"
git push origin main
```

### 步驟 4：設置 Secrets（FRED API Key）

1. 進入你的 GitHub 倉庫頁面
2. 點擊上方 **Settings** 頁籤
3. 左側選單點擊 **Secrets and variables → Actions**
4. 點擊 **New repository secret**
5. **Name** 填：`FRED_API_KEY`
6. **Secret** 填：你在步驟 1 申請的 API Key
7. 點擊 **Add secret**

### 步驟 5：啟用 GitHub Pages

1. 仍在倉庫 **Settings** 頁面
2. 左側選單點擊 **Pages**
3. **Source** 選擇 **GitHub Actions**
4. 完成！系統會自動偵測 `.github/workflows/daily-update.yml`

### 步驟 6：手動觸發第一次執行

1. 進入倉庫頁面，點擊上方 **Actions** 頁籤
2. 左側點擊 **Daily AI Liquidity Monitor Update**
3. 點擊右側 **Run workflow → Run workflow**
4. 等待約 2–3 分鐘，直到顯示綠色 ✅
5. 完成後，你的儀表板網址為：
   ```
   https://你的帳號.github.io/ai-liquidity-monitor-2026/
   ```

---

## 📊 數據來源與維度說明

| 維度 | 權重 | 自動數據來源 | 手動覆蓋欄位 |
|:---|:---|:---|:---|
| 1. 資金成本 | 20% | FRED: `DGS10` (10年債), `SOFR` | `dim1_override` |
| 2. 利差崩潰預警 | 25% | FRED: `BAMLH0A0HYM2` (高收益債利差) | `dim2_override` |
| 3. 日圓套利解除 | 15% | Yahoo Finance: `USDJPY=X` 30日波動率 | `dim3_override` |
| 4. 實體基建錯殺 | 25% | Yahoo Finance: `DLR`, `EQIX`, `COR` 平均殖利率 | `dim4_override` |
| 5. 地緣防火牆 | 15% | Yahoo Finance: `CNY=X` 30日波動率（**替代指標**） | `dim5_override` |

> ⚠️ **關於維度 5**：上海金交所 (SGE) vs 倫敦金 (LBMA) 價差無穩定免費 API，腳本目前以「人民幣離岸 30 日波動率」作為「境內資本恐慌/管制壓力」的替代指標。建議每日手動核對真實 SGE/LBMA 價差，並在 `config.json` 中覆蓋分數。

---

## 🔧 進階：手動覆蓋分數

若你對某個維度有更精確的數據（例如維度 5 的真實 SGE/LBMA 價差），可編輯 `config.json`：

```json
{
  "dim1_override": null,
  "dim2_override": null,
  "dim3_override": null,
  "dim4_override": null,
  "dim5_override": 10
}
```

- 設為 `null`：使用自動抓取值
- 設為 `0`, `5`, 或 `10`：強制使用該分數
- 修改後提交到倉庫，下次自動執行時會生效

---

## 🛠️ 常見問題

### Q1: FRED API Key 沒有設定會怎樣？
腳本會使用預設值（DGS10=4.2%, SOFR=5.33%, HY Spread=450 bps）繼續執行，但建議設定以獲得即時數據。

### Q2: Yahoo Finance 數據抓不到？
可能原因：
- Yahoo Finance 臨時限制 IP（GitHub Actions 的 IP 偶爾會被限）
- 股票代碼變更（REITs 下市或合併）
- **解決方案**：腳本會自動使用預設值，不會中斷。你也可以在 `config.json` 中手動覆蓋。

### Q3: 如何更改執行時間？
編輯 `.github/workflows/daily-update.yml` 中的 `cron`：
```yaml
- cron: '0 1 * * *'   # UTC 01:00 = 台灣 09:00
- cron: '0 22 * * *'  # UTC 22:00 = 台灣 隔日 06:00
```
[cron 語法參考](https://crontab.guru/)

### Q4: 可以部署到私有倉庫嗎？
GitHub Pages 免費版僅支援 **Public** 倉庫。若需私有，可改用 GitHub Actions 的 Artifact 下載，或部署到 Vercel/Netlify。

### Q5: 如何本地測試腳本？
```bash
# 1. 安裝依賴
pip install -r requirements.txt

# 2. 設定環境變數（Linux/Mac）
export FRED_API_KEY="你的APIKey"

# 3. 執行
python fetch_data.py

# 4. 用瀏覽器開啟 index.html
```

---

## 📁 檔案結構

```
ai-liquidity-monitor/
├── .github/
│   └── workflows/
│       └── daily-update.yml    # GitHub Actions 排程設定
├── fetch_data.py               # 主程式：抓數據 + 生成 HTML
├── config.json                 # 手動覆蓋設定（可選）
├── requirements.txt            # Python 依賴
├── index.html                  # 自動生成的儀表板（勿手動編輯）
└── README.md                   # 本說明文件
```

---

## 🖥️ 本地排程（Windows Task Scheduler）— 不依賴 GitHub Actions

GitHub Actions 是雲端全自動，但若你偏好本機單一 Python 腳本 + Windows 工作排程器（不假設任何 cron 框架）：

**1. 設環境變數（一次性）**
```powershell
# PowerShell（永久寫入使用者環境變數）
setx FRED_API_KEY "你的_FRED_KEY"
```

**2. 建一個 run_monitor.bat**（放在專案資料夾）
```bat
@echo off
cd /d "C:\path\to\ai-liquidity-monitor-2026"
python fetch_data.py
```
執行後會在同資料夾生成 / 覆蓋 `index.html`，用瀏覽器開它即可。

**3. 建立每日排程**
```powershell
# 每天台灣時間 09:00 執行；FRED 為美國數據，前一交易日值已定案
schtasks /create /tn "AI_Liquidity_Monitor" /tr "C:\path\to\ai-liquidity-monitor-2026\run_monitor.bat" /sc daily /st 09:00
```

**4.（可選）本機直接看**：把 `index.html` 拖進瀏覽器，或設成瀏覽器首頁。
若要對外公開網址，仍用 GitHub Pages 那條路徑（見上方步驟 5）。

> 註：本機排程需開機且該時段有登入 session。若要 24/7 不依賴本機開機，用 GitHub Actions 版本較穩。

---

## ⚠️ 免責聲明

本工具僅供**研究與教育用途**，不構成任何投資建議。所有數據來自公開 API，可能存在延遲或錯誤。**首次部署務必做裸投檢查**：確認健康面板顯示 LIVE 而非 FALLBACK，否則你看到的是預設值不是真數據。投資決策請自行判斷並諮詢專業顧問。

---

**作者**：基於 Edward Tsai（翟瑞楓）「穿境能力（Channel Fault Line）」框架與「2026 AI 流動性壓力測試指標」設計，由 Kimi AI 自動化實現。
