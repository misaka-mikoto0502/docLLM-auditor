# 用 LLM 审计 API 文档质量

把「API 条目解析（m5_parse_all）→ 全量验证（cm_run_product）→ Excel（gen_results_xlsx）」
与「单网页 demo（cm_demo_server）」三套能力的**最小必需集**装进本目录，脱离原项目后可直接复用。
全部路径经便携化改写——以本目录为根，**不依赖任何项目外路径**。

```
portable/
├─ README.md                      ← 本说明
├─ .tools/scripts/
│  ├─ m5_parse_all.py             ← API 条目解析(从 PDF 起)：PDF→pages_clean→segment 成单元（入口层）
│  ├─ cm_run_product.py           ← 全量验证入口
│  ├─ cmmatch_run.py              ← 被 cm_run_product 复用的辅助（断点续跑/裁决/印刷页标注）
│  ├─ run_all_products.py         ← 一键批量：遍历 audit/work 下所有产品缓存，逐个 cm_run_product→转 Excel
│  ├─ showcase_run.py             ← 展示用例：按票证定位真实单元→所属类别裁决→Excel（演示用全量走一遍主线）
│  ├─ cm_demo_server.py           ← demo 服务端：单网页选产品→选条目→逐类别/整目录判定
│  ├─ gen_results_xlsx.py         ← results.json → Excel（判定明细/confirmed违规/违规汇总 3 sheets）
│  ├─ gen_rule_doc.py             ← 重生成规则归类文档（可选）
│  └─ auditlib/                   ← 核心判定/解析库（仅 7 个必需模块，见下）
├─ spec/
│  ├─ spec_rules.json             ← 规则知识库（机器读主数据源）
└─ audit/
   ├─ cm_demo/index.html          ← demo 前端页面（服务端实时读取）
   └─ work/                       ← 产品 pages_clean 缓存放置区（用户自备，见使用）
```

## 每部分作用

| 文件/目录 | 作用 |
|---|---|
| `.tools/scripts/m5_parse_all.py` | **API 条目解析（从 PDF 起）**。`pdf_lib.extract_pages_clean`(PDF→pages_clean) + `segment.segment_apis`(→API 单元)，产物 `audit/work/m5/<svc>/{pages_clean.json,units.json}` + `m5_report.md`。PDF 放 `reference/API/`，依赖 `pip install pymupdf`。 |
| `.tools/scripts/cm_run_product.py` | **全量验证**。读 `spec/rule_categories.json` 的全部类别，对每个产品的 `pages_clean.json`（来自 m5_parse_all）里每条 API 逐类别做 LLM 判定。并发/断点续跑/熔断。产物 `results.{json,jsonl,md}`、`stats.json`、`run_log.txt`、`llm_call_log.jsonl`。 |
| `.tools/scripts/cmmatch_run.py` | 被上者 import（`task_key/load_done_keys/adjudicate_task/_annotate_printed_pages`）；也可独立跑示例产品全量。 |
| `.tools/scripts/run_all_products.py` | 批量驱动：自动发现 `audit/work`（含 `m5/`）下所有产品缓存，逐个跑 cm_run_product→gen_results_xlsx，全量一次跑完。`--only id1,id2` 可挑产品。 |
| `.tools/scripts/showcase_run.py` | 展示用例：内嵌示例票证（问题单「直接对比=是」实例），按端点定位示例服务真实单元→跑所属类别裁决→Excel，演示端到端主线。`TICKETS` 可自行增改。 |
| `.tools/scripts/cm_demo_server.py` | **demo**。`http.server` 单文件服务端，懒加载产品缓存，提供逐类别(路1)与整目录(路2)判定接口。仅 stdlib。 |
| `.tools/scripts/gen_results_xlsx.py` | 把全量验证 results.json 转成可筛选 Excel（依赖 openpyxl）。 |
| `.tools/scripts/gen_rule_doc.py` | 由 spec_rules.json 重生成规则归类 markdown（本目录未内置 md 产物，需要时运行它现造）。 |
| `.tools/scripts/auditlib/` | 判定/解析核心：`pdf_lib`(规则装载/URI解析)、`segment`(PDF解析→API条目)、`cmmatch`(per_rule 判定+双路召回)、`recall`(LLM 调用,本地 vLLM)、`detect`(客观探测器)、`deprec`(废弃排除)。**只含必需 7 模块**，剔除了 stage1/报表用到的 refine/report/resource_scope。 |
| `spec/spec_rules.json` | 规则知识库（判决依据的唯一数据源）。 |
| `spec/rule_categories.json` | 运行配置：哪些规则、分几类、每类哪些 —— 决定实际判定范围。 |
| `audit/cm_demo/index.html` | demo 前端；随服务端路径读取。 |
| `audit/work/` | 产品缓存放置区（见下）。 |

## 使用（迁移到别处后）

```bash
# 0) 准备（二选一）：
#   A. 只有 PDF —— 先把 API 参考 PDF 放到 reference/API/，跑条目解析（需 pip install pymupdf）
#      python .tools/scripts/m5_parse_all.py --pdfs svc-a      # 产出 audit/work/m5/svc-a/pages_clean.json
#   B. 已有 pages_clean 缓存 —— 直接把各产品清洗缓存放   audit/work/<产品>/pages_clean.json
#      （示例产品特例放 audit/work/pages_clean.json —— 见 cm_run_product.product_pages）
#   openpyxl 需 pip 安装（仅转 Excel 用）；demo/全量验证其余仅标准库。

# 1) 全量验证（单条目档全部类别；dry-run 先探路不改产物）
python .tools/scripts/cm_run_product.py sample --out audit/cm_sample --dry-run
python .tools/scripts/cm_run_product.py sample --out audit/cm_sample --workers 10 --max-fail 12 --pdf "路径/sample.pdf"

# 1b) 一键跑遍 audit/work 下所有产品（每个产物落到 audit/cm_ALL/<产品>/，含 xlsx）
python .tools/scripts/run_all_products.py --workers 8

# 2) 结果转 Excel
python .tools/scripts/gen_results_xlsx.py audit/cm_sample/results.json -o audit/cm_sample/results.xlsx

# 3) demo（浏览器打开 http://localhost:8000）
python .tools/scripts/cm_demo_server.py --port 8000
```

## 关键配置点（迁移时按需改）

- **LLM 连接**：`auditlib/recall.py` 顶部 `LLM_BASE_URL/LLM_MODEL`（现为本地 vLLM `http://<host>:8900/v1`）；已带直接代理绕过（`ProxyHandler({})`），局域网直连不卡死。密钥走环境变量 `LLM_KEY`（源码零凭据）。
- **判定范围**：`spec/rule_categories.json`（类别/规则划分）.
- **PDF 解析**：`m5_parse_all.py` 用 `pip install pymupdf`（fitz）取文本层；PDF 放 `reference/API/`，`TARGETS` 文件名字段按需增改。扫描版图片需另走 OCR 管道（本包不内置）。

## 与原项目的差别

- 仅保留本包所需文件；`auditlib` 只带 7 个模块（去掉 refine/report/resource_scope）。
- `cm_run_product.py`/`cmmatch_run.py`/`run_all_products.py` 写死的本机绝对路径已改为**相对自身目录**（`ROOT` 取到本包根）。
- 已清理与本项目无关的脚手架产物：`docs/`（SOP/features/decisions 模板）、`BACKLOG.md`、`.agent_history/`、`.office-claw/`、`__pycache__/`。
- 未含：PDF 原文（解析入口 `m5_parse_all.py` 已有，PDF 自备放 `reference/API/`）、pages_clean 大缓存（自备或由 m5_parse_all 现造）、规则_build 全量脚本、pdf 标注所需的 PDF、`全量清单/规则归类整合` 等 md（脚本现造）。
