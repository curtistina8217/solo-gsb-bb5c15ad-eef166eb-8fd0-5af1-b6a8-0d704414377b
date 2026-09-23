# 字幕断句校验台

基于 **FastAPI + SQLite + 原生前端** 的字幕断句校验工具。上传带时间码的
WebVTT/SRT 文件，设定每行字数、最小停留时间和阅读速度阈值后，系统会对跨
cue 文本做**动态规划断句**，必要时在相邻 cue 间**重分配边界时间**，并把
无法满足约束的**最小区段**标出并解释原因。

同时内置**术语审校**：维护按语言区分的术语表（规范词 / 允许变体 / 禁止替代
词），上传字幕后自动跨 cue 检测同一术语的大小写、全半角、标点与近似拼写
差异，逐项确认后批量生成术语修订 WebVTT，并可导出审校报告。

## 一键启动

```bash
docker compose up --build
```

启动后浏览器访问：

> http://localhost:8000

首页即上传入口；上传分析完成后在同页「校验结果」卡片依次使用问题筛选、
**片段合辑**与术语审校（均在结果页内，端口固定为宿主机 `8000`）。

SQLite 数据库文件位于容器内 `/data/subtitles.db`，通过 Compose 命名卷
`subtitle-data` 持久化；**数据库表在应用启动时自动初始化**，无需手工建表。

`examples/` 目录提供 `demo.vtt` / `demo.srt` 示例：前两条中文 cue 时间过紧
（会产生停留/阅读速度问题），第三条英文 cue 正常，可用于体验按语言筛选。
`examples/terms-demo.vtt` 包含同一术语的多种不一致写法，可用于体验术语审校。

## 功能

- **上传解析**：支持 WebVTT（`.vtt`）与 SubRip（`.srt`，自动转换处理），
  自动去除样式标签、NOTE/STYLE 块。
- **三项可配阈值**
  - 每行最大字数（默认 18）
  - 最小停留时间（秒，默认 0.8）
  - 阅读速度上限（字/秒，默认 12）
- **动态规划断句**：把所有 cue 展开为不可再分单元（拉丁连续词、单个
  CJK 字符、附着标点），以「违例数量最少 → 违例程度最小 → 停留冗余最小」
  为多级目标在全部可能切点上做 DP，因此每个被标出的问题都是**无法再切分
  的最小区段**。
- **跨 cue 边界重分配**：切点落在相邻 cue 之间时，自动在两 cue 的重叠区间
  （或间隙中点）重算时间边界；统计中展示拆分、合并与边界移动数量。
- **问题标注与原因解释**
  - `超长`：含无法拆分的连续词，任何切分都保留超长片段；
  - `停留过短`：相邻 cue 可借用时间不足，无法达到最小停留；
  - `阅读过快`：按阅读速度阈值所需时长超过实际可分配时长。
- **按语言筛选问题**：基于文字系统（中/日/韩/拉丁）与常用词的启发式语言
  检测，可在结果页下拉过滤；可勾选「只看有问题的条目」。
- **导出修订 WebVTT**：修订后的 cue 导出为标准 `.vtt`，问题原因以
  `NOTE` 行内嵌，可直接用于后续校对；在结果页选择语言后导出（或追加
  `?language=xx`），仅输出检测语言匹配的修订 cue（保持原顺序、时间码与
  文本，仅保留这些 cue 的 `NOTE`），无匹配 cue 时仍返回结构有效的空
  WebVTT；未选择语言时导出全部修订 cue。
- **片段合辑**：在结果页「片段合辑」卡片按指定顺序添加若干**互不重叠的
  半开时间窗** `[起点, 终点)`（端点以秒计，起点非负、终点须大于起点；
  窗口可相接、可上下调整顺序），从已保存的修订 cue 中取各窗口交集，裁到
  窗口边界后依次拼接为一条**新时间轴**：时间基准按窗口长度逐窗累加、
  窗内静音间隙原样保留，跨窗口的 cue 自动拆成多条，不生成零时长 cue。
  合辑表展示**原 cue 序号与合辑时间码**及文本、对应校验 `NOTE`；
  「导出合辑 WebVTT」输出独立的 `.compilation.vtt`（保留文本与对应校验
  `NOTE`）；空窗口集导出结构有效的空 WebVTT，原修订导出不受任何影响。
  窗口配置按任务存入 SQLite（覆盖式保存），从历史任务重开时自动载入，
  可继续编辑、重新生成。
- **历史任务**：每次校验存入 SQLite，可重新查看或再次导出。

## 术语审校

### 术语表（按语言区分）

在「术语表管理」卡片中维护术语，每条术语包含：

- **规范词**：统一后的目标写法（所有建议替换都指向规范词）；
- **允许变体**：可接受的写法，检测时不产生问题（大小写/全半角不敏感）；
- **禁止替代词**：显式禁用的写法，按「全半角不敏感、大小写敏感」精确匹配
  （如规范词 `WebVTT` 禁止 `webvtt`，不会误伤规范词自身）；
- **语言**：仅当 cue 的检测语言与术语语言一致，或术语设为「通用」时才参与
  检测。中文 cue 里的外来品牌名建议注册为「通用」。

### 跨 cue 不一致检测

上传字幕时（断句分析完成后）自动对**修订后 cue** 检测，维度包括：

| 类别 | 示例（规范词 → 字幕写法） | 归一方式 |
| ---- | ---- | ---- |
| 禁止替代词 | `WebVTT` → `webvtt`、`账号` → `帐号` | 全半角归一（保留大小写） |
| 大小写不一致 | `WebVTT` → `WEBVTT` | NFKC + 大小写折叠 |
| 全半角不一致 | `WebVTT` → `ＷｅｂＶＴＴ` | NFKC 全角转半角 |
| 标点不一致 | `WebVTT` → `Web VTT` / `e-mail` → `email` | 去除空格、连字符、间隔号等连接标点 |
| 近似拼写 | `WebVTT` → `WebVTs`、`人工智能` → `人工知能` | 有界编辑距离（仅对归一长度 ≥ 4 的术语启用；纯前后缀延伸如 `WebVTTX` 视为派生词，不告警） |

每条疑似项显示类别、规范词、所在**修订 cue 序号与来源原 cue 序号**、时间码、
**高亮上下文**、问题写法与建议替换。可逐项「接受 / 忽略 / 恢复待确认」，也可
一键全部接受或全部忽略；术语表更新后可点击「重新检测」（会重置确认状态）。

### 导出

- **导出术语修订 WebVTT**：仅把已接受项批量替换到修订后 cue（同 cue 多处按
  偏移从后往前应用），文件名形如 `xxx.terms.vtt`，响应头
  `X-Term-Replacements` 给出实际替换数；
- **导出审校报告 CSV**：UTF-8 BOM（Excel 可直接打开），含规范词、语言、类别、
  问题写法、建议替换、cue 序号/时间码、确认状态、原因与上下文；
  追加 `?format=json` 可得同内容 JSON。

### 快速体验

```text
术语 1  语言=通用   规范词=WebVTT  禁止替代词=webvtt
术语 2  语言=中文   规范词=账号     禁止替代词=帐号
术语 3  语言=中文   规范词=人工智能
```

上传 `examples/terms-demo.vtt`（阈值保持默认即可），结果区「术语审校」
会列出 6 项不一致（禁止替代词 ×2、全半角、标点、近似拼写 ×2）。

> 说明：检测在修订后 cue 上进行；若断句恰好把一个多 token 术语切到两个 cue
> 中，则该写法不会被识别，可适当放宽每行字数后重新分析。

## API 摘要

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/analyze` | multipart 上传 `file`、`max_chars`、`min_dwell`、`max_cps`（响应含 `term_findings` 术语疑似项数） |
| GET  | `/api/submissions` | 历史任务列表 |
| GET  | `/api/submissions/{id}` | 单次任务完整结果（支持 `?language=en` 过滤） |
| GET  | `/api/submissions/{id}/export` | 下载修订后的 WebVTT（支持 `?language=en`：仅导出该检测语言的修订 cue 及其校验 NOTE） |
| PUT  | `/api/submissions/{id}/compilation` | 保存（覆盖）片段合辑窗口配置（JSON：`{"windows":[{"start":s,"end":e},…]}`，按数组顺序拼接），返回合辑 cue（含原 cue 序号、合辑时间码、校验 NOTE） |
| GET  | `/api/submissions/{id}/compilation` | 读取已保存的合辑配置与结果（未配置返回 404；空窗口集返回空合辑） |
| GET  | `/api/submissions/{id}/compilation-export` | 下载独立合辑 WebVTT（`xxx.compilation.vtt`；空窗口集为结构有效的空 WebVTT） |
| GET  | `/api/terms` | 术语表列表（支持 `?language=zh` 过滤，通用术语一并返回） |
| POST | `/api/terms` | 新增术语（JSON：`language`/`canonical`/`variants`/`forbidden`/`note`） |
| PUT  | `/api/terms/{id}` | 更新术语 |
| DELETE | `/api/terms/{id}` | 删除术语 |
| GET  | `/api/submissions/{id}/term-findings` | 该任务的术语疑似不一致项 |
| POST | `/api/submissions/{id}/term-check` | 按当前术语表重新检测（重置确认状态） |
| POST | `/api/terms/findings/status` | 批量更新确认状态（`ids` + `pending`/`accepted`/`dismissed`） |
| GET  | `/api/submissions/{id}/term-export` | 应用已接受替换，导出术语修订 WebVTT |
| GET  | `/api/submissions/{id}/term-report` | 导出审校报告 CSV（`?format=json` 返回 JSON） |
| GET  | `/api/health` | 健康检查 |
| GET  | `/docs` | 自动生成的接口文档 |

## 本地开发（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/subtitles.db uvicorn app.main:app --reload --port 8000
```

## 目录结构

```
app/
├── main.py          # FastAPI 路由与静态文件挂载（含片段合辑接口）
├── compilation.py   # 半开窗口校验 + cue 裁切/新时间轴拼接
├── segmenter.py     # 单元切分 + DP 断句 + 边界重分配 + 问题生成
├── subtitles.py     # VTT/SRT 解析、VTT 导出、语言检测
├── terms.py         # 术语表模型 + 多层归一匹配 + 跨 cue 不一致检测引擎
├── database.py      # SQLite 初始化与持久化（含术语表/术语发现/合辑配置）
└── static/          # 原生 HTML/CSS/JS（无构建步骤）
tests/smoke_test.py        # FastAPI TestClient 端到端冒烟测试
tests/term_engine_check.py # 术语引擎独立逻辑校验（仅需标准库）
tests/compilation_check.py  # 片段合辑裁切/拼接/导出逻辑校验（仅需标准库）
examples/                  # demo.vtt / demo.srt / terms-demo.vtt 示例字幕
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

```bash
pip install -r requirements.txt
DB_PATH=/tmp/test.db python3 tests/smoke_test.py   # 端到端（含术语审校）
python3 tests/term_engine_check.py                 # 术语引擎纯逻辑校验（无需依赖）
python3 tests/compilation_check.py                  # 片段合辑纯逻辑校验（无需依赖）
```
