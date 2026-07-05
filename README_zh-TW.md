<!-- 語言：**繁體中文** · [English](./README.md) -->

# hermes-plugin-mem4

> [English](./README.md) · **繁體中文（Traditional Chinese）**

**mem4** 是給 [Hermes](https://github.com/webdevtodayjason/hermes-plugin-template) 用的「四層路由記憶 provider」。它**補強**內建記憶、而不是取代它：把永遠載入的「熱區」維持得很小，把細節推到按需讀取的冷區微檔，再加上一套支援中文的全文召回索引 —— 全部走本機檔案,**零外部依賴**,而且**停用即降級、零殘留**。

它被打包成一個可獨立安裝的 Hermes 外掛,因此核心的 `hermes update` 不會蓋掉它(外掛住在核心樹之外)。

---

## mem4 是什麼?

Hermes 內建記憶的運作方式:`memory` 工具把 `MEMORY.md` / `USER.md` 寫到 `$HERMES_HOME/memories/`,並在每個 session 開場注入 system prompt。這個「永遠載入的熱區」是權威來源,但它會愈長愈大,而且每一輪對話都要為它的完整大小付出 token 成本。

mem4 把一套**四層路由記憶設計**(ADR-018)包裝成 Hermes 的 `MemoryProvider`,採**並存 / 補強(coexist / augment)**模式:

| 層級 | 角色 | 存放位置 |
|------|------|----------|
| **L0 — 熱區(hot)** | 永遠載入、每個 session 注入 system prompt 的權威記憶。 | 內建 `MEMORY.md` / `USER.md`(Hermes 所有,mem4 **絕不碰**) |
| **L1 — 快取(cache)** | 冷區讀取內容的近期快照,留在本機供快速重讀。 | `$HERMES_HOME` 下的本機快取 |
| **L2 — 微檔(microfile)** | 按需的冷知識,L0 缺該細節時才用「路由碼」讀取。 | `$HERMES_HOME/mem4/<code>.md`(人類可讀,對 git / Obsidian 友善) |
| **L3 — 冷(cold)** | 批次整併 / 歸檔的材料。 | mem4 自有檔案,由 Dream 壓實(見下) |

核心概念:**L0 維持精簡**(在內建檔案之外只多一小段路由圖例),模型需要 L2/L3 細節時再用工具按需拉取,或由本機召回索引預先取回。內建記憶始終是唯一真相來源;mem4 純粹是一個**加法的**「讀取增強 + 召回」層。

---

## 特色

- 🟢 **並存 / 補強,絕不取代。** mem4 **只讀取**內建 `MEMORY.md` / `USER.md`,並把觀察到的寫入鏡射到它**自己**位於 `$HERMES_HOME/mem4/` 的檔案。它從不寫入、搬移或刪除內建記憶。這是程式碼強制的硬保證(路徑穿越防護讓鏡射在物理上無法逃出 mem4 目錄)。
- 🟢 **停用 = 乾淨降級、零殘留。** 從 `config.yaml` 拿掉 `memory.provider: mem4`,Hermes 直接回到純內建記憶。`$HERMES_HOME/mem4/` 目錄變成惰性,可隨時刪除。因此「更好」是一個**低風險的加法宣稱**:有實測效益就留、沒有就回退 —— 內建路徑始終完好。
- 🟢 **local-file backend、零外部依賴。** 預設(在此精簡底盤中也是唯一)的 backend 只用 Python **標準函式庫 + SQLite** —— 沒有 pip 套件,而且在對話熱路徑上**不做任何 MCP / 網路呼叫**。
- 🟢 **支援中文的雙表 FTS5 召回。** mem4 擁有自己的 SQLite FTS5 資料庫,同時索引對話回合與冷區微檔。它用**雙表**方案讓中文搜尋真的能用(見 [召回](#召回mem_searchprefetch) 與 [限制](#限制))。
- 🟢 **Dream 整併:事件觸發、無外部 cron。** mem4 自有冷區的整併完全在 provider 內以純程式碼執行 —— 由「寫入次數門檻」或「session 邊界的過期底線」觸發。沒有計時器、沒有要顧的背景常駐程序。
- 🟢 **永遠可重建。** 每個派生層(FTS5 索引、鏡射快照)都能用 `hermes mem4 rebuild` 從真相來源檔案重建 —— 非破壞性,而且絕不為了寫入去讀內建檔案。
- 🟢 **用量測、不用嘴。** 內建稽核器 + A/B 對照臂 + 受控量測工具,用資料證明價值,並有硬性的 SHIP / ROLLBACK 判準。見 [受控量測](#ab-受控量測)。

---

## 安裝

mem4 是**選用(opt-in)**外掛。先安裝、再啟用,最後把 Hermes 的 memory provider 指向它。

```bash
# 1. 從 GitHub 安裝
hermes plugins install sam7894604/hermes-plugin-mem4

# 2. 啟用外掛
hermes plugins enable mem4
```

接著在 `config.yaml` 打開它:

```yaml
memory:
  provider: mem4
  mem4:
    backend: local-file      # 預設;精簡底盤中唯一的 backend
    dream:
      enabled: true          # 事件觸發整併(預設開)
      threshold: 25          # 累積幾次記憶寫入後觸發一次事件整併
      staleness_days: 7      # session 邊界時,若距上次整併超過這麼多天則整併
    recall:
      prefetch_cap: 2000     # prefetch 每輪最多注入的字元數
    audit:
      enabled: false         # 選用:每次召回/路由事件記一行 JSONL
    arm: experiment          # experiment | baseline(見受控量測)
```

要**停用**,直接拿掉 `memory.provider: mem4` 即可,不留任何殘留。

> **需求。** mem4 在 Hermes host 內執行(它會從執行中的行程 import `agent.memory_provider` / `tools.registry`)。預設 backend 只需 Python 標準函式庫 —— 不用 pip 安裝。Python ≥ 3.10。

---

## 工具

啟用時(且不在 `baseline` A/B 臂),mem4 對外提供兩個工具,並在 system prompt 放一小段永駐的路由圖例。

### `mem_route(code)`

當永遠載入的 L0 缺該細節時,用**路由碼**讀取某個 L2/L3 冷區微檔。

- 路由碼:`sys`(系統/環境)、`fam`(人物/家庭)、`vlt`(vault/知識)、`adr`(架構決策)、`proto`(協定/流程)。開頭的 `§` 可省略。
- 讀取 `$HERMES_HOME/mem4/<code>.md`。每個結果前面都附一個**新鮮度標記**(`[fresh: local-file]`、`[STALE: …]` 或 `[built-in only]`)。
- 未命中**絕不報錯** —— 回傳一個標記(`built-in memory remains authoritative`),讓 agent 優雅地退回 L0。

### `mem_search(query, limit=5)`

對 mem4 索引做全文召回 —— 涵蓋過去的對話回合**與**冷區微檔 —— 找回那些已經離開熱區的知識。**支援英文與中文**。回傳帶來源的排序片段;當歷史索引還在追趕時,會附一個 `[backfill in progress]` 註記。

---

## 召回(`mem_search`、`prefetch`）

mem4 擁有自己的 SQLite FTS5 資料庫(`$HERMES_HOME/mem4/recall.db`),同時索引回合與微檔。它沿用上游的雙表模式讓 CJK 搜尋能用:

- **`docs_fts`(unicode61)** 負責英文 / BM25 相關性,加上 **`docs_fts_trigram`(trigram)** 負責 CJK。
- `_contains_cjk()` 檢查會把 CJK 查詢導向 trigram 表。任何短於 3 字元的 CJK token(trigram 的最小單位)會退回逐 token 的 `LIKE` 掃描。
- 若本機 SQLite build 缺 trigram tokenizer,CJK 查詢降級為 `LIKE` —— **絕不硬性失敗**。
- 排序在相關性之上疊了一層**時間衰減權重**(半衰期 30 天),讓較新的材料勝過同等相關的較舊材料。

除了 `mem_search` 工具外,還有兩個介面:

- **`prefetch(query)`** —— 回合開場的召回。**只做本機 I/O**(SQLite + 檔案,絕不 MCP/網路 —— 它在熱路徑上同步執行),並限制在 `recall.prefetch_char_cap` 個字元(預設 2000)。
- **`sync_turn(...)`** —— 索引每個完成的回合,並過濾(最小長度、剝除工具輸出;儲存層以內容雜湊去重)。

歷史 backfill 透過持久化在 `.mem4_state.json` 的游標**可續跑**,所以重啟能從中途接續。真實部署會注入一個 session 歷史來源;沒有的話,只索引微檔 / 鏡射。

---

## Dream 整併

Dream 完全在 provider 內以**純程式碼執行 —— 無外部 cron**。觸發條件:

- **事件 / 門檻** —— 每次內建記憶寫入都是一個訊號;跨過 `dream.threshold` 次新寫入就觸發一次整併。
- **過期底線** —— 在 session 邊界,若距上次整併超過 `dream.staleness_days` 天*且*有待處理訊號,就整併。
- **閒置略過** —— 沒有待處理訊號 ⇒ 沒東西可整併 ⇒ 略過。純閒置不需要計時器。

整併會壓實 mem4 自有的鏡射日誌(去重),並在改寫前**把壓實前的原檔歸檔到 `_mirror/_archive/`**,所以什麼都不會遺失。它**只碰 mem4 自有的 L2/L3** —— 絕不碰內建熱區。設 `dream.enabled: false` 會讓 Dream 變成完全的 no-op。

> **部署備註。** 若你以前把 Dream 當成獨立 cron / `jobs.json` 條目在跑,當 mem4 內建 Dream 後請把那條目退役 —— 否則會對同一份 L2/L3 重複執行。

---

## 與內建記憶的關係

整個設計一句話講完:**內建記憶是唯一真相來源;mem4 是一個加法的「讀取增強 + 召回」層,只觀察與讀取它,永不回寫。** 具體而言:

- mem4 會**讀取** `MEMORY.md` / `USER.md`(例如量測常駐熱區大小),但**絕不寫入**它們。
- 內建記憶的寫入會被**鏡射**到 `$HERMES_HOME/mem4/_mirror/<target>.md`(mem4 所有),讓召回能涵蓋它們 —— 原檔則位元不差、原封不動。
- 當 mem4 不可用、設定錯誤(例如指向未實作的 backend)或被停用時,provider 進入非啟用狀態,Hermes 改用純內建記憶。**降級是預設的失敗模式**,而不是半殘的壞掉載入。

---

## `hermes mem4 rebuild`

```bash
hermes mem4 rebuild
```

從真相來源檔案(微檔 + 鏡射日誌)清除並重建召回索引,然後重跑歷史 backfill。非破壞性;絕不為了寫入去讀內建記憶檔。這就是「派生層永遠可重建」的保證 —— 召回索引與其他派生狀態隨時能重建,所以鏡射漂移可自癒。

---

## `hermes mem4 refine` —— §3 縮限式放寬(熱區縮小)

```bash
hermes mem4 refine                 # dry-run:只印提案(預設)
hermes mem4 refine --apply         # 備份 → 抽微檔 → 原子改寫 MEMORY.md
hermes mem4 refine --restore [ts]  # 從備份還原 MEMORY.md(省略 ts 取最新)
```

「絕不回寫內建 `MEMORY.md`」鐵律的**唯一、顯式、可還原**例外。它解析 `MEMORY.md` 的 `§<code>` 段落(純啟發式、零 LLM、零依賴),把每段抽成 `$HERMES_HOME/mem4/<code>.md` 冷區微檔,並把 `MEMORY.md` 濃縮成短標頭 + 無 § 歸屬核心 + 路由索引 —— 可量測地縮小每輪必載的熱區。

安全保證:日常 `on_memory_write` 路徑**永不**回寫(不變);自動路徑(首次 bootstrap、Dream④)只把提案刷新到 `mem4/_refine_proposal.md`,**永不** apply;`--apply` 會先把 `MEMORY.md` 備份到 `mem4/_refine_backups/MEMORY-<UTCts>.md`,絕不靜默覆蓋既有微檔(也一併備份),原子寫入(`.tmp` → `os.replace`;失敗則原檔零改動),並可用 `--restore` 完整還原。無 `§` 標記時降級用 markdown 標題,再無則用大小分塊。詳見 [`docs/refine-section3-policy.md`](docs/refine-section3-policy.md)。

---

## A/B 受控量測

> **先講誠實。** 下面的數字來自**合成 / fixture** 資料 —— 它們是*機制證明*,不是正式環境結果。真實命中率需要部署到真實工作負載並蒐集實際使用量;同一套工具屆時再對真實資料執行。凡非在你自己流量上量測的數字,一律標為 **示範(demonstrative)**。

價值用資料量測,不用估計。內建三樣工具:

1. **稽核器 Auditor**(`memory.mem4.audit.enabled: true`)—— 每次召回/路由/prefetch 事件寫一列到本機 SQLite(`$HERMES_HOME/mem4/audit.db`,表 `audit_events`):query、arm、route(`fts`/`trigram`/`like`)、hit/hit_estimated、tool_called、注入字元/token、成對的 baseline/mem4 注入 token,以及 `paired_diff`。用 `hermes mem4 audit`、`Auditor.query(sql)` 或任何 SQLite 客戶端查詢。工具呼叫的*未命中*是**精測**;L0 命中率(未用工具的回合)是離線**推估**,並如實標註。*(Baserow 907 匯出已停用/預設關閉;既有的 `audit.jsonl` 會被一次性匯入 `audit.db`。)*
2. **A/B 對照臂**(`memory.mem4.arm: experiment | baseline`)。在 `baseline`,mem4 有載入但**所有面向 agent 的介面全關**(無工具、無 system prompt 圖例、無 prefetch 注入),因此熱區/工具表面與純內建一致,而召回儲存仍可量測。用同一份工作負載各跑一臂再比較。
3. **受控量測工具**(`hermes mem4 eval`)—— 三層設計,讓結果不被流量隨機性混淆,並回報**分布**(最小/中位/最大),而非單一數字:
   - **確定性離線重放**(主要,零隨機)—— 同一組固定輸入對 baseline 與 mem4 各重放一次;逐項:標準命中(**精測**)、注入 token(**精測**)、路由。
   - **成對反事實**(paired counterfactual)—— 逐查詢同時記錄「純內建*會*注入多少」與「mem4 *實際*注入多少」,回報成對差異分布。
   - **常駐成本**(resident cost)—— session 開場的注入大小:baseline(整份 `MEMORY.md`)對 mem4(短圖例)。

### Fixture / 示範結果

- **常駐熱區大小** —— ADR-018 設計在其 fixture 資料集上量到 **2,175 → 665 字元,約 −69%** 的縮減。*(示範 / fixture 量測 —— 你的縮減幅度取決於你的 `MEMORY.md` 有多大;機制是「注入一小段圖例、而非整個熱區」。)*
- **冷知識召回** —— 期望方向是 **experiment > baseline**(baseline ≈ 0,因為 baseline 召不回已離開熱區的東西)。此處不宣稱任何正式環境命中率數字。
- **每查詢 token 差** —— 由量測工具回報為成對差異*分布*,不是單一數字。

### SHIP / ROLLBACK 判準

量測工具硬寫了一道判準閘(`gate()` 對每條準則印 PASS/FAIL)。**唯有全部成立**才 **SHIP**:

1. mem4 召回了 baseline 召不回的冷知識(**標準命中 Δ ≥ 30%**),**且**
2. 每查詢的淨 token 縮小了(工具回讀 + prefetch 成本**沒有**吃掉熱區省下的量),**且**
3. 常駐熱區縮小了。

否則就 **ROLLBACK**(拿掉 `memory.provider`)。傷害對話穩定度的延遲/錯誤同樣是回退觸發條件。統管原則:*所有宣稱都立在實測資料上;未達標不宣稱。*

---

## 限制

- **中文雙字詞是弱點。** SQLite FTS5 預設的 `unicode61` tokenizer 不做 CJK 分詞,所以 CJK 搜尋靠 trigram 表 —— 但 **trigram 查詢需要 ≥ 3 字元**。雙字中文詞(如「部署」)會退回 `LIKE` 掃描,那是子字串比對、非排序相關性。召回仍能運作,但雙字查詢的精確度不如三字以上的查詢。(LIKE 回退在真實語料上的命中率是待量測項目。)
- **無 trigram tokenizer → CJK 降級為 `LIKE`。** 若你的 SQLite build 缺 trigram tokenizer,CJK 搜尋仍能透過 `LIKE` 運作,只是沒有 trigram 排序。
- **歷史 backfill 需要來源。** 沒有注入 session 歷史來源時,只索引微檔與鏡射日誌 —— 不含你完整的過往對話歷史。
- **精簡底盤。** 只實作了 `local-file` backend;remote-vault / local-vault 拓撲為保留項。真實資料量測由營運者把關(部署 + 蒐集使用量)。

---

## 開發與測試

外掛的 `mem4` 套件以其正規的頂層名稱 `mem4` 被 import。`mem4` 原始碼與來源相同、未經修改;獨立 repo 只加了封裝、文件與測試框架。

```bash
# 跑完整測試套件(每個測試檔在自己的隔離子行程中執行)。
python run_tests.py

# 或直接跑單一檔案。
python -m pytest tests/test_mem4_recall.py -q
```

**為什麼要用 runner?** 這些測試會操作 SQLite FTS5 虛擬表與一個背景 backfill 執行緒,它們的原生/全域狀態在單一長壽 interpreter 內無法在測試之間乾淨重置 —— 把所有檔案塞進同一個行程一起跑可能讓 interpreter 崩潰。`run_tests.py` 採**每檔一個行程**(對齊上游 Hermes 套件),任何檔案失敗就以非零碼結束。

測試框架(`tests/conftest.py`)會為兩個 Hermes host 模組(`agent.memory_provider`、`tools.registry`)安裝**輕量 stub** —— 但**僅在真正的 Hermes 套件無法 import 時**才裝。因此單元測試能在沒裝 Hermes 的乾淨 checkout 跑,在真正的 Hermes 樹裡則兼做一次 host 整合冒煙測試。測試不修改任何 mem4 原始碼。

目前狀態:**54 個測試,全綠**(每檔隔離)。

---

## 授權

[MIT](./LICENSE) © 2026 sam7894604。

設計依據:ADR-018 四層路由記憶架構,以及《2026-07-04 四層記憶包裝為 Hermes Provider 設計 spike》。外掛結構遵循 [hermes-plugin-template](https://github.com/webdevtodayjason/hermes-plugin-template) 慣例。
