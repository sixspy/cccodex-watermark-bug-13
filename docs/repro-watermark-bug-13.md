# 复现与二验步骤 · watermark-bug-13

题目：**窗界过期后写印接口仍放行；追索读到未生效版；差额账入库漂。**
本纸给出改前复现与改后验收两条路径，均在本机 SQLite 上完成，不依赖 compose。

约定：
- 仓库根目录执行，虚拟环境已按 `requirements.txt` 安装依赖。
- 日期口径以 `LAB_TODAY` 为准，未设置时取系统当天（默认窗界截止 2026-11-01，恒早于今天，
  因此**装起来就是「窗界已过期」**）。
- 数据库路径由 `DATABASE_PATH` 指定；复现请用新文件，避免旧库干扰。

```bash
export DATABASE_PATH=$(mktemp -u /tmp/wm13-XXXX.db)
export LAB_TODAY=2026-11-19
```

## 一、改前复现（三条症状）

下面三条在**改前**都会「静默成功」，这正是缺陷所在。

1. **窗界过期后仍能写印**
   窗界读数（`GET /api/meta` 的 `window`）显示窗界截止 `2026-11-01`，早于今天，即已过期。
   此时对任一 `待盖印` 批次 `POST /api/batches/{id}/stamp`，旧实现直接写库并回 200，
   `run_batches.status` 变成 `已盖印`。**窗外静默成功。**

2. **追索读到未生效版**
   旧 `stamp_batch` 取模板只按 `status='现行'`，**不看生效窗**；同一张表换版后，
   新版会把业务日仍在旧版生效窗内的批次顶上。旧 `trace_downstream` 也只按水印值回查，
   不复验盖印用的是哪一版、该版在业务日是否生效。

3. **差额账入库漂**
   旧 `run_gap_report` 只看 `status='已盖印'`，把窗外补印也算进已盖印，
   `expected/stamped/gap` 三数与真实进度不符，并原样写进 `gap_reports`、
   且没有记录对账时使用的窗界，事后无法复盘。

一条脚本验证（改前）：

```python
from app.db import init_db
from app.services.batches import stamp_batch
from app.services.gaps import run_gap_report

init_db()
print(stamp_batch(1, "WM-DEMO"))      # 改前：返回已盖印（不应发生）
print(run_gap_report("2026-11-19"))   # 改前：窗外印计进已盖印
```

## 二、改后验收（逐条对账）

### 1. 四处同看：窗源目录 / 版式卡片 / 落标附页 / 过期进度

启动 `python run.py`，登录 `admin / 123456` 进入 `/app/`：

- **顶栏读数条**（过期进度）：`窗界已过期 / 有效 / 未生效` 芯片 + `起 ~ 止` +
  `读数源` + `第 N 版`，右侧进度条给出「已过天数 / 总天数 / 剩余天数」。
- **窗源目录**页：页首 `窗源：<读数源>` 芯片亮明这份窗界从哪来；每张表一张
  **版式卡片**，列出各版 `vN · 状态 · 生效窗`，卡片头部芯片与顶栏同一份读数。
- **落标附页**：点任一批次的「落标」后出现，含三枚**读数芯片**
  （窗界状态 / 业务日是否在窗内 / 页面预判能否落标）与「读数芯片」明细行
  （窗界区间 + 版本 + 读数源）。

四处读的都是同一个后端函数（`app/services/windows.py: describe_window`），
不存在各页各写一套窗界文案。**只改文案不改行为不算通过**——下面每条都在库上验。

### 2. 窗界过期 → 落标被拦（中文短句）

默认库即是过期态，直接落标：

```bash
curl -b cj -X POST localhost:7000/api/batches/1/stamp \
  -H 'Content-Type: application/json' -d '{"watermark_value":"WM-DEMO"}'
```

期望：`HTTP 409`，`{"ok": false, "error": "窗界已过期，禁止落标"}`；
随后 `GET /api/batches/1` 仍是 `待盖印`。**没有静默成功。**

窗界未生效回 `窗界未生效，禁止落标`；业务日在窗外回
`批次业务日在窗外，禁止落标`。

### 3. 开窗后落标，一次裁定、同源可核

```bash
curl -b cj -X POST localhost:7000/api/window -H 'Content-Type: application/json' \
  -d '{"window_start":"2026-11-01","window_end":"2026-11-30"}'
curl -b cj -X POST localhost:7000/api/batches/1/stamp \
  -H 'Content-Type: application/json' -d '{"watermark_value":"WM-OK"}'
```

期望 200，且响应 `batch` 带 `stamped_window_start/end`（本次裁定用的窗界快照）、
`template_version`（按业务日生效窗选出的版式）与 `stamp_state=valid`。

本机库可核：

```bash
sqlite3 "$DATABASE_PATH" \
  "select id,start_date,end_date,revision,origin from window_state;
   select run_code,status,template_version,stamped_window_start,stamped_window_end from run_batches;"
```

- 窗界只有一行 `window_state`；
- 每条盖印都留了当次窗界快照，事后可复算。

### 4. 双人调度争用改窗（排他锁）

```bash
python - <<'PY'
import threading
from app.services.windows import update_window
def w(day):
    update_window("2026-11-01", f"2026-11-{day:02d}", actor="scheduler")
ts=[threading.Thread(target=w,args=(d,)) for d in range(16,22)]
[t.start() for t in ts]; [t.join() for t in ts]
PY
sqlite3 "$DATABASE_PATH" \
  "select count(*) from window_state; select count(*) from window_events;"
```

期望：`window_state` 恒为 1 行；`window_events` 为 6 条改窗记录（另加 1 条初始化记录）。
改窗走 `BEGIN IMMEDIATE` 排他事务，版本号自增；`expected_revision` 做乐观校验，
窗界被别人改过时回 `窗界已变更，请刷新后重试`。

### 5. 落标与顺藤查各复验一轮，不再吃窗外新印

```bash
# 1) 窗内正常落标
curl -b cj -X POST localhost:7000/api/window -H 'Content-Type: application/json' \
  -d '{"window_start":"2026-11-01","window_end":"2026-11-30"}'
curl -b cj -X POST localhost:7000/api/batches/2/stamp \
  -H 'Content-Type: application/json' -d '{"watermark_value":"WM-CHECK"}'
# 2) 人工注入一枚「过期后补印」的脏数据（模拟旧 bug 产物）
sqlite3 "$DATABASE_PATH" \
  "update run_batches set stamped_window_end='2026-11-15', stamped_at='2026-11-19 09:00:00' where id=2;"
# 3) 追索与对账都不认它
curl -b cj -X POST localhost:7000/api/trace \
  -H 'Content-Type: application/json' -d '{"watermark_value":"WM-CHECK"}'
curl -b cj -X POST localhost:7000/api/gap-reports \
  -H 'Content-Type: application/json' -d '{"as_of_date":"2026-11-19"}'
```

期望：

- 追索回 `409 水印值的盖印批次复验未通过`；
- 对账摘要带 `窗界外/失效盖印 1`，这枚印**不计入**已盖印，`gap_reports.window_snapshot`
  记下本次使用的窗界；
- 批次列表该行显示 `盖印失效` 芯片，悬停可见原因（`缺少盖印时窗界快照` /
  `盖印发生在窗界过期后` / `盖印版式在该业务日未生效`）。

### 6. 自动化回归

```bash
pytest -q
```

覆盖：出厂窗界过期即拦印、窗界未生效拦印、窗外业务日拦印、版式按业务日生效窗选版
（11 月批次不会被 12 月才生效的 v2 顶上）、盖印留窗界快照、追索复验剔除、
对账不漂且带窗界快照、改窗排他锁与版本冲突、页面四处读数同源。

## 三、实现落点（便于二验对照）

| 关注点 | 位置 |
|--------|------|
| 窗界唯一读数 / 裁定 | `app/services/windows.py`（`describe_window` / `evaluate_batch`） |
| 落标入口（写事务 + 条件更新） | `app/services/batches.py` |
| 盖印复验（落标 / 追索 / 对账共用） | `app/services/verify.py` |
| 追索复验一轮 | `app/services/trace.py` |
| 差额账同源口径 + 窗界快照 | `app/services/gaps.py` |
| 唯一窗界行 + 事件行 | `app/db.py`（`window_state` / `window_events`） |
| 改窗接口 | `app/main.py`（`GET/POST /api/window`） |
| 四处读数前端 | `static/js/app.js`（`renderWindowBar` / `renderTemplates` / `renderStampPanel`） |
| 回归用例 | `tests/test_window.py`、`tests/test_window_api.py` |

## 四、读数瞬间的选择

题面给了两种可行读数，本项目取**提交瞬间重读现行窗**：

- 打开页时取一次窗界快照，只用于展示与页面预判（顶栏、窗源目录、版式卡片、落标附页）；
- 落标在同一个写事务里重读 `window_state` 一次裁定，页面停留期间窗界漂移不会让旧读数放行；
- 改窗支持 `expected_revision` 乐观校验，页面带着打开时的版本提交，窗界被别人改过就回 409；
- 页面预判与后端裁定不一致时，页面立即重读窗界并显示后端的中文短句。

这样「窗界」与「可落标」始终出自同一次读数，不会出现页面说可落、后端说不可落的两套口径。
