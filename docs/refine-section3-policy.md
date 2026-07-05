# mem4 refine — §3 縮限式放寬政策與安全保證

> 狀態：已實作（`mem4/refine.py`、`hermes mem4 refine`）。
> 對應 vault 設計文件：`技術/架構決策/2026-07-04_四層記憶包裝為Hermes-Provider設計spike`
> 的 §3 / §8.3。**vault 尚未同步此檔**（本 session TurboVault MCP 未連線）——
> 待反向同步進 vault。

## 背景：原本的鐵律

mem4 以 **coexist / augment** 模式運作：它補強內建 `MEMORY.md` / `USER.md`，
但**日常永不回寫**這兩個內建檔（design spike §3 / §8.3）。`on_memory_write`
只把寫入鏡射到 mem4-owned 的 `_mirror/`，Dream④ 只壓實 `_mirror/` 底下的檔案，
兩者都不碰內建檔。停用 mem4 即乾淨退回純內建記憶、零殘留。

這條鐵律的代價：內建 `MEMORY.md` 只會長大、不會縮小，熱區（每輪必載）成本無上限。

## §3 縮限式放寬：唯一、顯式、可還原的例外

`refine` 是那條鐵律**唯一**的放寬。它把 `MEMORY.md` 的長段落抽進 mem4 冷區微檔，
把 `MEMORY.md` 濃縮成「短標頭 + 無 § 歸屬的必要核心 + 路由索引」，藉此**可量測地
縮小熱區**。放寬被以下界線嚴格框住：

1. **只有顯式 `refine --apply` 能改寫 `MEMORY.md`。**
   日常路徑（`on_memory_write`）永不回寫的硬保證**不變**。
2. **自動路徑永不 apply。** 首次 bootstrap 與 Dream④ 只呼叫 `refresh_proposal()`，
   把提案寫到 mem4-owned 的 `_refine_proposal.md`；**從不**改寫內建檔。
3. **預設 `--dry-run`**：只印提案，不碰任何檔。
4. **絕不靜默覆蓋**：
   - apply 前一定先把 `MEMORY.md` 備份到
     `~/.hermes/mem4/_refine_backups/MEMORY-<UTCts>.md`。
   - 若某段的 microfile 已存在且內容不同，先把舊微檔備份到
     `_refine_backups/microfiles-<UTCts>/<code>.md` 再寫。
5. **原子寫入**：先寫 `MEMORY.md.refine-tmp`，再 `os.replace`。中途失敗則清掉
   tmp、**原檔零改動**（且備份已在步驟 4 完成）。
6. **可還原**：`refine --restore [<ts>]` 從備份還原 `MEMORY.md`（省略 `<ts>` 取最新）。

## 精煉策略：啟發式優先，零 LLM、零依賴

骨幹是純啟發式的結構化拆分（這步本身即達成可量測的熱區縮小）：

1. **動態解析 `§<code>` 段落標記**（依實際標記，不寫死段名）。
2. 每段 → `~/.hermes/mem4/<code>.md` 的 L2 微檔。
3. 濃縮 `MEMORY.md` = 短標頭 + 無 § 歸屬的必要核心 + 路由索引
   （每個 `§code` 一行摘要，指向對應微檔，附 `mem_route(code)` 用法）。
4. **降級鏈**：無 `§` 標記 → 用 markdown `##` 標題分段；再無 → 用大小分塊。
   非 ascii（如中文）標題無法產生合法 route code 時，退化為 `s1`/`s2`…，
   摘要仍保留原標題文字。

LLM 去重／濃縮列為**選配、預設 OFF**，不在本次實作範圍。

## 觸發矩陣

| 路徑 | 動作 | 會改寫 MEMORY.md？ |
|---|---|---|
| `on_memory_write`（日常） | 鏡射到 `_mirror/` | ❌ 永不 |
| 首次 bootstrap | `refresh_proposal()` | ❌ 只寫提案檔 |
| Dream④（session_start） | `refresh_proposal()` | ❌ 只寫提案檔 |
| `refine --dry-run`（預設） | 印提案 | ❌ |
| `refine --apply`（顯式） | 備份 → 抽微檔 → 原子改寫 | ✅（唯一） |
| `refine --restore [<ts>]` | 從備份還原 | ✅（還原用途） |

## 稽核

`Auditor.record_refine()` 在每次 apply 記一筆 `kind="refine"` 事件（沿用既有
`audit_events` schema，無 migration）：精煉前後 `MEMORY.md` 的 byte / token 數、
微檔數、archived 數。`paired_diff = before_tokens − after_tokens` 即本次精煉從
熱區移走的 token 數。

## 注意：coexist 部署下的節省是「潛在」而非「已實現」

如果部署維持 coexist（`MEMORY.md` 不瘦身），內建與 mem4 兩者都會注入，mem4 是加法，
熱區並未真的變小。`refine --apply` 正是把「潛在節省」轉成「已實現節省」的那一步——
它**真的**縮小了每輪必載的 `MEMORY.md`。這也是為什麼 apply 必須顯式且可還原。
