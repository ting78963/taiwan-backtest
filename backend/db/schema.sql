-- ============================================================
-- Taiwan Backtest System — Database Schema v3
--
-- 設計原則：
--   1. 原始資料永久保存，不因研究假說改變而刪除
--   2. 所有衍生資料（事件、回測）綁定版本號，可重算
--   3. 前端 RUN 只讀 DB，絕不觸發 FinMind 抓取
--   4. 缺日期才補抓，Engine 升版才重建事件
--   5. 資料收集門檻(collection_threshold) ≠ 研究篩選門檻(research_threshold)
--   6. Key confirmed_time ≠ key source_time，避免 look-ahead
--   7. timeout 出場必須用實際截止 close，不得估算
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 管理層：版本與庫存追蹤
-- ════════════════════════════════════════════════════════════

-- 記錄哪些日期的原始資料已完整存入
-- collection_threshold：資料收集門檻（決定是否抓全天 K），與研究門檻分離
CREATE TABLE IF NOT EXISTS data_inventory (
    id                   BIGSERIAL PRIMARY KEY,
    date                 DATE        NOT NULL UNIQUE,
    fetch_status         VARCHAR(20) NOT NULL DEFAULT 'done',
    stocks_fetched       INT         DEFAULT 0,
    stocks_skipped       INT         DEFAULT 0,
    stocks_error         INT         DEFAULT 0,
    collection_threshold NUMERIC(6,4),   -- 資料收集用的漲幅門檻（預設2.5%）
    -- 已移除 min_early_vol：成交量不作為收集門檻
    fetched_at           TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT data_inventory_date_unique UNIQUE (date)
);

-- Engine 版本登記表
CREATE TABLE IF NOT EXISTS engine_versions (
    version_id   BIGSERIAL PRIMARY KEY,
    engine_type  VARCHAR(30) NOT NULL,
    version_tag  VARCHAR(20) NOT NULL,
    description  TEXT,
    is_current   BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (engine_type, version_tag)
);

-- 記錄哪個日期已用哪個 engine_version 跑過事件辨識
CREATE TABLE IF NOT EXISTS event_runs (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE        NOT NULL,
    key_version     VARCHAR(20) NOT NULL,
    attack_version  VARCHAR(20) NOT NULL,
    outcome_version VARCHAR(20) NOT NULL,
    run_status      VARCHAR(20) DEFAULT 'done',
    keys_found      INT,
    attacks_found   INT,
    ran_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, key_version, attack_version, outcome_version)
);

-- ════════════════════════════════════════════════════════════
-- Layer 1：原始 1 分 K（永久保存，絕不刪改）
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS market_data (
    id         BIGSERIAL PRIMARY KEY,
    date       DATE        NOT NULL,
    stock_id   VARCHAR(10) NOT NULL,
    time       TIME        NOT NULL,
    open       NUMERIC(10,2),
    high       NUMERIC(10,2),
    low        NUMERIC(10,2),
    close      NUMERIC(10,2),
    volume     BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, stock_id, time)
);

CREATE INDEX IF NOT EXISTS idx_md_date_stock ON market_data(date, stock_id);
CREATE INDEX IF NOT EXISTS idx_md_stock_date ON market_data(stock_id, date);
CREATE INDEX IF NOT EXISTS idx_md_date       ON market_data(date);

-- ════════════════════════════════════════════════════════════
-- Layer 2：每日 Context
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_context (
    id                     BIGSERIAL PRIMARY KEY,
    date                   DATE        NOT NULL,
    stock_id               VARCHAR(10) NOT NULL,
    prev_close             NUMERIC(10,2),
    prev_day_volume        BIGINT,
    -- 早盤視窗（由 EARLY_START/EARLY_END 參數決定，預設 09:00~09:10）
    early_high_price       NUMERIC(10,2),
    early_high_time        TIME,           -- 最高價第一次出現的時間（≠ confirmed time）
    early_high_pct         NUMERIC(8,4),
    early_volume           BIGINT,         -- 原始早盤成交量，永久保存
    volume_ratio           NUMERIC(8,4),   -- 向後相容主欄位（= volume_ratio_at_0910）
    -- 量比兩件組（09:10 當下可真實觀察，無 look-ahead）
    -- 注意：early_high_price / early_high_pct 已代表 09:00~09:10 的最高價與漲幅，此處不重複保存
    cumulative_volume_at_0910 BIGINT,          -- 09:00~09:10 累積量（張，對應前端 todayVolZhang）
    volume_ratio_at_0910      NUMERIC(8,4),   -- 累積量 ÷ 前5日均量（對應前端 volRatio）
    -- 篩選旗標
    -- passes_price_filter：早盤漲幅 >= collection_threshold（資料收集用）
    -- passes_research_filter 由回測時動態計算，不存於此
    passes_price_filter    BOOLEAN,        -- 是否達到資料收集門檻（collection_threshold）
    passes_volume_filter   BOOLEAN DEFAULT TRUE,  -- 永遠 TRUE，保留欄位供未來擴充
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_dc_date       ON daily_context(date);
CREATE INDEX IF NOT EXISTS idx_dc_date_stock ON daily_context(date, stock_id);
CREATE INDEX IF NOT EXISTS idx_dc_pass       ON daily_context(date, passes_price_filter);

-- ════════════════════════════════════════════════════════════
-- Layer 3：Key Events
-- key_source_time：最高價第一次出現的時間（有 look-ahead 的時間，僅記錄用）
-- key_confirmed_time：早盤視窗結束後才能確認的時間（Attack Detection 的起始點）
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS key_events (
    key_id             BIGSERIAL PRIMARY KEY,
    date               DATE        NOT NULL,
    stock_id           VARCHAR(10) NOT NULL,
    key_version        VARCHAR(20) NOT NULL DEFAULT 'V1',
    -- key_price = early_high_price（09:00~09:10 最高實際成交價，就是那面「牆」）
    -- early_high_pct >= threshold 只決定這面牆值不值得研究，不是牆本身
    key_price          NUMERIC(10,2) NOT NULL,
    key_source_time    TIME        NOT NULL,  -- early_high_price 第一次出現的時間（key_source_time）
    key_confirmed_time TIME        NOT NULL,  -- 09:10（視窗結束，Attack Detection 的起始點）
    -- 相容舊欄位（= key_confirmed_time，向後相容舊查詢）
    key_created_time   TIME,
    key_high           NUMERIC(10,2),
    key_low            NUMERIC(10,2),
    detection_basis    TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, stock_id, key_version, key_price)
);

CREATE INDEX IF NOT EXISTS idx_ke_date_stock ON key_events(date, stock_id);
CREATE INDEX IF NOT EXISTS idx_ke_version    ON key_events(key_version);

-- ════════════════════════════════════════════════════════════
-- Layer 4：Attack Events（含 V1A/V1B 永久欄位）
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS attack_events (
    attack_id        BIGSERIAL PRIMARY KEY,
    key_id           BIGINT      NOT NULL REFERENCES key_events(key_id) ON DELETE CASCADE,
    date             DATE        NOT NULL,
    stock_id         VARCHAR(10) NOT NULL,
    attack_version   VARCHAR(20) NOT NULL DEFAULT 'V1',
    attack_number    INT         NOT NULL,

    -- 時間
    start_time       TIME        NOT NULL,
    end_time         TIME        NOT NULL,
    bars_used        INT         NOT NULL,

    -- 價格
    start_price      NUMERIC(10,2),
    key_price        NUMERIC(10,2),
    attack_high      NUMERIC(10,2),
    attack_low       NUMERIC(10,2),

    -- 量能（兩個版本，永久保存）
    attack_volume    BIGINT,               -- = attack_volume_v1b（向後相容主欄位）
    attack_volume_v1a BIGINT,              -- 首根 bar 量（最保守，只算第一根碰 Key 的量）
    attack_volume_v1b BIGINT,              -- 整段 Attack 所有 bar 量總和

    -- 多版本判定
    is_touch         BOOLEAN DEFAULT FALSE,
    is_upward        BOOLEAN DEFAULT FALSE,
    is_cross         BOOLEAN DEFAULT FALSE,
    is_close_above   BOOLEAN DEFAULT FALSE,

    -- 突破
    crossed_key            BOOLEAN DEFAULT FALSE,
    closed_above_key       BOOLEAN DEFAULT FALSE,
    attack_high_above_key  NUMERIC(10,2),

    -- C 值（V1B 版，向後相容主欄位）
    c21              NUMERIC(8,4),
    c31              NUMERIC(8,4),
    c32              NUMERIC(8,4),
    c41              NUMERIC(8,4),
    -- C 值（V1A 版，首根量計算）
    c31_v1a          NUMERIC(8,4),
    c41_v1a          NUMERIC(8,4),

    -- 動態量比（Attack end_time 當下累積量 ÷ 前一交易日全天量）
    volume_ratio_at_attack NUMERIC(8,4),  -- cumulative_vol_through_end_time / prev_day_volume
    estimated_volume_growth_at_attack NUMERIC(10,2),  -- (cum_vol * F(end_time) / prev_day_vol - 1) * 100

    -- 進場候選（entry_at_trigger = NULL，避免 look-ahead）
    entry_at_trigger     NUMERIC(10,2),    -- 永遠 NULL（保留欄位供相容），研究輸出不使用
    entry_at_bar_close   NUMERIC(10,2),
    entry_next_open      NUMERIC(10,2),
    entry_next_close     NUMERIC(10,2),

    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (key_id, attack_version, attack_number)
);

CREATE INDEX IF NOT EXISTS idx_ae_date_stock ON attack_events(date, stock_id);
CREATE INDEX IF NOT EXISTS idx_ae_key_id     ON attack_events(key_id);
CREATE INDEX IF NOT EXISTS idx_ae_version    ON attack_events(attack_version);
CREATE INDEX IF NOT EXISTS idx_ae_attack_num ON attack_events(attack_number);

-- ════════════════════════════════════════════════════════════
-- Layer 5：Outcome Data（含實際截止價，禁止估算 timeout 報酬）
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS outcome_data (
    id               BIGSERIAL PRIMARY KEY,
    attack_id        BIGINT      NOT NULL REFERENCES attack_events(attack_id) ON DELETE CASCADE,
    outcome_version  VARCHAR(20) NOT NULL DEFAULT 'V1',
    entry_mode       VARCHAR(30) NOT NULL,
    entry_price      NUMERIC(10,2),
    entry_time       TIME,

    -- 各截止時間的實際 exit_price（截止時間那根 K 的 close）
    -- timeout 出場使用此欄位計算報酬，嚴禁用 (MFE+MAE)/2 估算
    exit_price_5m    NUMERIC(10,2),   -- 進場後第 5 根 K 的 close
    exit_price_10m   NUMERIC(10,2),   -- 進場後第 10 根 K 的 close
    exit_price_0959  NUMERIC(10,2),   -- 09:59 那根 K 的 close（或最後一根）
    exit_price_1030  NUMERIC(10,2),
    exit_price_1130  NUMERIC(10,2),
    exit_price_close NUMERIC(10,2),   -- 13:30 收盤 close

    -- 各截止時間的實際報酬（= exit_price / entry_price - 1）
    return_5m        NUMERIC(8,4),
    return_10m       NUMERIC(8,4),
    return_0959      NUMERIC(8,4),
    return_1030      NUMERIC(8,4),
    return_1130      NUMERIC(8,4),
    return_close     NUMERIC(8,4),

    -- MFE / MAE（各截止時間內的最大有利/不利波動）
    mfe_5m           NUMERIC(8,4),
    mae_5m           NUMERIC(8,4),
    mfe_10m          NUMERIC(8,4),
    mae_10m          NUMERIC(8,4),
    mfe_0959         NUMERIC(8,4),
    mae_0959         NUMERIC(8,4),
    mfe_1030         NUMERIC(8,4),
    mae_1030         NUMERIC(8,4),
    mfe_1130         NUMERIC(8,4),
    mae_1130         NUMERIC(8,4),
    mfe_close        NUMERIC(8,4),
    mae_close        NUMERIC(8,4),

    -- TP/SL 首次觸發時間（全天記錄）
    first_plus050_time  TIME,
    first_plus075_time  TIME,
    first_plus100_time  TIME,
    first_plus125_time  TIME,
    first_plus150_time  TIME,
    first_plus200_time  TIME,
    first_minus025_time TIME,
    first_minus050_time TIME,
    first_minus075_time TIME,
    first_minus100_time TIME,

    -- TP/SL 是否在 5m/10m 內觸發（供相對根數截止使用）
    -- TRUE = 觸發在第 5/10 根以內，FALSE = 未觸發或在第 5/10 根之後
    tp050_within_5m   BOOLEAN DEFAULT FALSE,
    tp050_within_10m  BOOLEAN DEFAULT FALSE,
    tp075_within_5m   BOOLEAN DEFAULT FALSE,
    tp075_within_10m  BOOLEAN DEFAULT FALSE,
    tp100_within_5m   BOOLEAN DEFAULT FALSE,
    tp100_within_10m  BOOLEAN DEFAULT FALSE,
    tp125_within_5m   BOOLEAN DEFAULT FALSE,
    tp125_within_10m  BOOLEAN DEFAULT FALSE,
    tp150_within_5m   BOOLEAN DEFAULT FALSE,
    tp150_within_10m  BOOLEAN DEFAULT FALSE,
    tp200_within_5m   BOOLEAN DEFAULT FALSE,
    tp200_within_10m  BOOLEAN DEFAULT FALSE,
    sl025_within_5m   BOOLEAN DEFAULT FALSE,
    sl025_within_10m  BOOLEAN DEFAULT FALSE,
    sl050_within_5m   BOOLEAN DEFAULT FALSE,
    sl050_within_10m  BOOLEAN DEFAULT FALSE,
    sl075_within_5m   BOOLEAN DEFAULT FALSE,
    sl075_within_10m  BOOLEAN DEFAULT FALSE,
    sl100_within_5m   BOOLEAN DEFAULT FALSE,
    sl100_within_10m  BOOLEAN DEFAULT FALSE,

    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (attack_id, outcome_version, entry_mode)
);

-- Migration：為已存在的 outcome_data 新增 within 欄位
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp050_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp050_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp075_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp075_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp100_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp100_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp125_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp125_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp150_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp150_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp200_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp200_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl025_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl025_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl050_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl050_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl075_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl075_within_10m BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl100_within_5m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl100_within_10m BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_od_attack_id ON outcome_data(attack_id);

-- ════════════════════════════════════════════════════════════
-- Layer 6：回測管理
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              BIGSERIAL PRIMARY KEY,
    run_name            VARCHAR(100),
    engine_version      VARCHAR(20)  NOT NULL DEFAULT 'V1',
    key_version         VARCHAR(20)  NOT NULL DEFAULT 'V1',
    attack_version      VARCHAR(20)  NOT NULL DEFAULT 'V1',
    outcome_version     VARCHAR(20)  NOT NULL DEFAULT 'V1',
    params              JSONB        NOT NULL,
    date_from           DATE         NOT NULL,
    date_to             DATE         NOT NULL,
    -- research_threshold：回測篩選用，≥ collection_threshold
    research_threshold  NUMERIC(6,4) DEFAULT 0.035,
    status              VARCHAR(20)  NOT NULL DEFAULT 'pending',
    total_combos        INT,
    total_events        INT,
    total_trades        INT,
    error_message       TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_br_status  ON backtest_runs(status);
CREATE INDEX IF NOT EXISTS idx_br_created ON backtest_runs(created_at DESC);

-- ════════════════════════════════════════════════════════════
-- Layer 7：逐筆交易結果
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS backtest_trades (
    trade_id          BIGSERIAL PRIMARY KEY,
    run_id            BIGINT       NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    attack_id         BIGINT       NOT NULL REFERENCES attack_events(attack_id),
    strategy_id       VARCHAR(100) NOT NULL,

    -- 事件特徵（冗餘存，匯出 CSV 不需 JOIN）
    date              DATE,
    stock_id          VARCHAR(10),
    prev_close        NUMERIC(10,2),
    early_high_pct    NUMERIC(8,4),   -- 新增：供 research_threshold 篩選驗證
    volume_ratio      NUMERIC(8,4),
    key_price         NUMERIC(10,2),
    attack_number     INT,
    attack_volume     BIGINT,         -- V1B
    attack_volume_v1a BIGINT,         -- V1A（新增）
    attack_definition VARCHAR(20),
    c21               NUMERIC(8,4),
    c31               NUMERIC(8,4),   -- V1B
    c31_v1a           NUMERIC(8,4),   -- V1A（新增）
    c32               NUMERIC(8,4),
    c41               NUMERIC(8,4),

    -- 進場
    entry_mode        VARCHAR(30),
    entry_time        TIME,
    entry_price       NUMERIC(10,2),

    -- 出場條件設定
    tp_pct            NUMERIC(6,4),
    sl_pct            NUMERIC(6,4),
    exit_time_limit   VARCHAR(10),

    -- 出場結果
    exit_time         TIME,
    exit_price        NUMERIC(10,2),
    exit_reason       VARCHAR(20),    -- 'hit' / 'timeout' / 'excluded'

    -- hit=True → TP 目標值；hit=False → 截止時實際 close 報酬
    observed_return_pct NUMERIC(8,4),

    -- MFE/MAE
    mfe               NUMERIC(8,4),
    mae               NUMERIC(8,4),

    -- TP/SL 先後順序
    tp_hit_time       TIME,
    sl_hit_time       TIME,
    tp_hit_first      BOOLEAN,

    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bt_run_id    ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_strategy  ON backtest_trades(run_id, strategy_id);
CREATE INDEX IF NOT EXISTS idx_bt_date      ON backtest_trades(date);
CREATE INDEX IF NOT EXISTS idx_bt_stock     ON backtest_trades(stock_id);
CREATE INDEX IF NOT EXISTS idx_bt_attack_id ON backtest_trades(attack_id);

-- ════════════════════════════════════════════════════════════
-- 初始資料：Engine 版本登記
-- ════════════════════════════════════════════════════════════

INSERT INTO engine_versions (engine_type, version_tag, description, is_current)
VALUES
    ('key',     'V1', 'Key = 09:00~09:10 最高價；key_source_time=最高點時間，key_confirmed_time=視窗結束時間', TRUE),
    ('attack',  'V1', 'Upward Attack from below Key；V1A=首根量，V1B=整段量；entry_at_trigger=NULL', TRUE),
    ('outcome', 'V1', '逐分鐘 MFE/MAE；各截止時間實際 exit_price；timeout 用實際 close', TRUE)
ON CONFLICT (engine_type, version_tag) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- Migration helper：ALTER TABLE 新增欄位（idempotent）
-- 已存在的資料庫執行 init_db() 時，這些語句確保欄位存在
-- ════════════════════════════════════════════════════════════

-- key_events：新增 source/confirmed 時間分離
ALTER TABLE key_events ADD COLUMN IF NOT EXISTS key_source_time    TIME;
ALTER TABLE key_events ADD COLUMN IF NOT EXISTS key_confirmed_time TIME;

-- 若現有資料庫有舊的 key_created_time 欄位，補填新欄位
UPDATE key_events
SET key_source_time    = key_created_time,
    key_confirmed_time = key_created_time
WHERE key_source_time IS NULL AND key_created_time IS NOT NULL;

-- attack_events：新增 V1A/V1B 欄位
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS attack_volume_v1a BIGINT;
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS attack_volume_v1b BIGINT;
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS c31_v1a           NUMERIC(8,4);
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS c41_v1a           NUMERIC(8,4);

-- 向後相容：舊資料 v1a = v1b = attack_volume
UPDATE attack_events
SET attack_volume_v1b = attack_volume,
    attack_volume_v1a = attack_volume
WHERE attack_volume_v1b IS NULL AND attack_volume IS NOT NULL;

-- outcome_data：新增實際 exit_price 與 return 欄位
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_5m    NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_10m   NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_0959  NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_1030  NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_1130  NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS exit_price_close NUMERIC(10,2);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_5m        NUMERIC(8,4);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_10m       NUMERIC(8,4);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_0959      NUMERIC(8,4);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_1030      NUMERIC(8,4);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_1130      NUMERIC(8,4);
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS return_close     NUMERIC(8,4);

-- backtest_runs：新增 research_threshold 欄位
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS research_threshold NUMERIC(6,4) DEFAULT 0.035;

-- backtest_trades：新增 V1A、early_high_pct 欄位
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS attack_volume_v1a BIGINT;
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS c31_v1a           NUMERIC(8,4);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS early_high_pct    NUMERIC(8,4);

-- outcome_data：新增 TP 6%~9.9% 欄位
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus600_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus700_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus800_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus900_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus990_time TIME;

-- attack_events：新增 volume_ratio_at_attack 和 estimated_volume_growth_at_attack
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS volume_ratio_at_attack NUMERIC(8,4);
ALTER TABLE attack_events ADD COLUMN IF NOT EXISTS estimated_volume_growth_at_attack NUMERIC(10,2);

-- data_inventory：改名 price_threshold → collection_threshold
ALTER TABLE data_inventory ADD COLUMN IF NOT EXISTS collection_threshold NUMERIC(6,4);
-- UPDATE data_inventory SET collection_threshold = price_threshold WHERE collection_threshold IS NULL; -- 舊欄位已不存在，略過

-- outcome_data 新增 TP/SL 擴充欄位（idempotent）
-- TP targets
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus250_time  TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus300_time  TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus400_time  TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_plus500_time  TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp250_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp250_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp300_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp300_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp400_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp400_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp500_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS tp500_within_10m  BOOLEAN DEFAULT FALSE;
-- SL levels 擴充
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_minus125_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_minus150_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_minus200_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_minus250_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS first_minus300_time TIME;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl125_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl125_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl150_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl150_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl200_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl200_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl250_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl250_within_10m  BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl300_within_5m   BOOLEAN DEFAULT FALSE;
ALTER TABLE outcome_data ADD COLUMN IF NOT EXISTS sl300_within_10m  BOOLEAN DEFAULT FALSE;
