# 台股盤中結構回測系統

> Upward Key Attack Research Engine

## 架構

```
前端 (GitHub Pages) → 後端 API (Render) → PostgreSQL (Render)
                              ↑
                      FinMind API（1分K）
```

## 部署步驟

### 1. 建立 GitHub Repo

```bash
git init
git add .
git commit -m "init: taiwan backtest system"
git remote add origin https://github.com/ting78963/taiwan-backtest.git
git push -u origin main
```

### 2. 部署後端到 Render

1. 登入 [render.com](https://render.com)
2. New → Blueprint → 選擇你的 repo
3. render.yaml 會自動建立：
   - **Web Service**：`taiwan-backtest-api`
   - **PostgreSQL**：`taiwan-backtest-db`
4. 在 Web Service 的 Environment 手動填入：
   - `FINMIND_TOKEN`：你的 FinMind token

### 3. 部署前端到 GitHub Pages

1. GitHub repo → Settings → Pages → Source: GitHub Actions
2. push 到 main 後自動部署

### 4. 連結前端與後端

在 `frontend/index.html` 最上方的 script 區塊加入：

```html
<script>
  window.API_BASE = 'https://taiwan-backtest-api.onrender.com';
</script>
```

或直接部署時透過 GitHub Actions 環境變數注入。

---

## 使用流程

### Step 1：抓取資料
- 設定日期範圍（如 2024-01-01 ~ 2024-03-31）
- 早盤漲幅門檻預設 3.5%
- 早盤最低成交量預設 4000 張
- 按「開始抓取資料」→ 背景執行，頁面顯示進度

### Step 2：事件辨識
- 設定相同日期範圍
- 按「執行事件辨識」→ 依序執行：
  - Key Detection（找早盤最高價作為 Key）
  - Attack Engine（找 Upward Key Attack 事件）
  - Outcome Engine（計算 MFE/MAE）

### Step 3：回測分析
- 選擇策略矩陣（全選 = 幾百種組合全跑）
- 按「開始回測」→ 背景執行

### Step 4：查看結果
- 結果報表頁：查看各策略組合統計
- 匯出 CSV 頁：下載三份原始資料

---

## 資料庫結構

| Layer | Table | 說明 |
|-------|-------|------|
| 1 | `market_data` | 原始 1 分 K（永久保存） |
| 2 | `daily_context` | 昨收、量比、早盤高點 |
| 3 | `key_events` | Key Price 事件 |
| 4 | `attack_events` | Attack 事件（含 C 值） |
| — | `outcome_data` | MFE/MAE 結果 |
| — | `backtest_runs` | 回測任務記錄 |
| — | `backtest_trades` | 逐筆交易結果 |

---

## 核心設計原則

1. **原始資料層永久保存**，不因研究假說改變而刪除
2. **量比計算唯一來源** → `events/volume_ratio.py`
3. **禁止 Look-ahead Bias**：所有訊號只能使用當下以前的資料
4. **多版本 Attack 定義並存**：Touch / Upward / Cross / Close Confirm
5. **C 值保存原始數值**，不截斷為是/否
6. **TP/SL 按時間順序判斷誰先觸發**
7. **不自動刪除表現差的策略組合**，全部保存供分析
