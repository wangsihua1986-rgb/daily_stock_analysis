# 市场支持与边界

## 日本/韩国个股 suffix-only MVP（Issue #1718，Refs #1718）

当前阶段支持手动输入日本、韩国股票的 Yahoo Finance 后缀代码，进入既有个股分析、历史保存和基础报告展示链路。Web 自动补全内置一批常用日股/韩股种子索引，支持按 suffix 代码、中英文名称或常用别名搜索。

支持格式：

- 日本：`7203.T`、`6758.T`
- 韩国 KOSPI：`005930.KS`
- 韩国 KOSDAQ：`035720.KQ`

约束与边界：

- 手动输入裸代码时会先检索本地/远程股票池；若 `005930`、`000660` 等裸码命中 `005930.KS`、`000660.KS` 等日韩条目，则按命中的市场提交分析；若股票池未命中，仍按既有 6 位数字代码规则默认落到 A 股语义，并保留为可追踪的跨市场歧义边界。
- 日股/韩股 suffix 识别已集中到共享市场代码工具，数据源路由、Prompt 市场识别、交易日历和股票索引裸码解析复用同一组规则，减少后续市场扩展时的规则漂移。
- 日股/韩股日线和基础实时/近实时行情只走 `YfinanceFetcher`，不尝试 AkShare、Tushare、Efinance、Pytdx、Baostock 等 A 股专属数据源；yfinance 报价会尽量带上 `market`、`currency`、`data_quality`、`missing_fields` 等质量元数据。
- 基本面复用既有 offshore yfinance 轻量路径；A 股专属资金流、龙虎榜、板块等能力按 `not_supported` 降级，offshore 基本面上下文也会标记 provider、as_of、data_quality 和缺失块。
- 报告 Prompt 已增加日股/韩股市场语义，避免套用 A 股涨跌停、北向资金、龙虎榜、融资融券等概念。
- 交易日历注册 `jp: XTKS / Asia/Tokyo` 与 `kr: XKRX / Asia/Seoul`。若本地 `exchange-calendars` 版本缺少对应日历，既有 fail-open/fail-closed 语义保持不变。

兼容性与回退说明（针对结构化检测命中项）：

- `#1815` 本次仅新增 `yfinance` 报价/基本面上下文中的可选字段元数据（`market`、`currency`、`data_quality`、`missing_fields`、`provider`），不改动 LLM `provider`、`model`、`base_url`、配置 Schema、运行时迁移分支、数据库 schema 与消息协议版本；外部 provider/API 仍沿用既有链路与 fallback。
- 兼容/回退证据：
  - 官方约定参考：[LiteLLM OpenAI-compatible](https://docs.litellm.ai/docs/providers/openai_compatible)、[OpenAI Chat Completion API](https://platform.openai.com/docs/api-reference/chat)；
  - 运行时配置不清空路径：`tests/test_system_config_service.py::test_runtime_env_fallback_does_not_persist_llm_fields_on_save`、`tests/test_system_config_service.py::test_runtime_env_fallback_does_not_override_saved_provider_and_base_url_settings`、`tests/test_system_config_service.py::test_import_desktop_env_merges_keys_without_deleting_unspecified_values`、`tests/test_config_env_compat.py`；
  - 清理行为与可见提示：`tests/test_system_config_service.py::test_update_alphasift_enable_does_not_rewrite_llm_fields`、`tests/test_system_config_service.py::test_update_preserves_masked_secret`；
  - 实际回退路径：恢复 `.env` 备份中的 provider/model/base_url 值，或回退提交。
- 运行时配置语义说明：`MARKET_REVIEW_REGION`、`MARKET_REVIEW_COLOR_SCHEME` 与 Web 市场选择入口只影响大盘复盘输入/展示；不触发 provider/model/base_url 重写、运行时配置清理、路由迁移分支。
- 回退方式：若新增元数据字段在某端产生兼容问题，可先忽略这些字段并按既有市场判定+行情展示链路运行；必要时回滚本次提交或通过移除 `jp/kr` `MarketSymbol` 及路由扩展恢复旧行为。

不承诺项：

- 不承诺实时行情；Yahoo Finance 数据可能延迟或字段缺失。
- 不承诺完整基本面、行业/板块、市场宽度或涨跌家数。JP/KR 大盘复盘 v1 仅提供主要指数、新闻线索与模板/LLM 复盘，不提供日韩市场宽度或板块排行。
- 不承诺完整日韩全市场股票列表；Web 自动补全当前仅覆盖仓内种子索引中的常用标的（已扩充至各 30 只左右的头部标的），未命中时仍可手动输入 suffix 代码。
- Portfolio 允许 JP/KR 账户、交易和持仓快照进入现有链路，但会将账户/持仓快照标记为 `data_quality=partial`，并通过 `limitations` 明确 `realtime_quote_best_effort`、`fx_and_cost_basis_partial`、`sector_and_risk_metrics_limited`；不承诺 JPY/KRW 汇率、成本、市值、行业集中度或组合风险指标完整口径。

回滚方式：移除 `jp/kr` 市场识别、交易日历注册、YFinance 路由扩展、Web/API 类型放行、`scripts/stock_index_seeds/` 日韩种子索引，并删除本文档中的能力声明。

## 日本/韩国大盘复盘 v1（Issue #1815 Phase 2）

大盘复盘 `MARKET_REVIEW_REGION` 新增 `jp` 与 `kr`，并纳入 `both` 的多市场顺序：`cn,hk,us,jp,kr`。

支持范围：

- `jp`：通过 Yahoo Finance 获取日经225 `^N225` 与东证指数 `^TOPX`，输出日股大盘复盘。
- `kr`：通过 Yahoo Finance 获取 KOSPI `^KS11` 与 KOSDAQ `^KQ11`，输出韩股大盘复盘。
- Web 设置页可选择 `jp` / `kr`；交易日检查会按 `XTKS / Asia/Tokyo` 与 `XKRX / Asia/Seoul` 过滤 `both` 中当日开市市场。
- 复盘策略、新闻搜索词、Prompt 市场语义和中英文通知标题均按 JP/KR 独立 profile 处理。

边界：

- JP/KR 大盘复盘 v1 不提供涨跌家数、涨跌停、行业/板块排行或资金流统计；结构化 payload 中 `breadth` 仍只在有市场宽度数据时出现。
- 单一 JP/KR 指数拉取失败按既有 yfinance fail-open 逻辑跳过，不拖垮其它指数或其它市场。
- 如果 `exchange-calendars` 缺少对应交易所日历，继续沿用既有交易日 fail-open/fail-closed 语义。

回滚方式：从 `MARKET_REVIEW_REGION` 合法值、Web 设置枚举、MarketProfile/MarketStrategy、`_MARKET_REVIEW_MARKETS` 和本文档中移除 `jp` / `kr`。

## 日本/韩国 Portfolio / Market Light 边界（Issue #1815 Phase 3）

Portfolio：

- JP/KR 账户、交易、现金流水和公司行动 API 保持可创建/查询，以便用户记录持仓。
- 持仓快照对 JP/KR 账户和持仓明确返回 `data_quality=partial` 与 `limitations`，表示实时价、汇率/成本基准、行业与组合风险指标均为 best-effort 或受限口径。
- 当前不新增 JPY/KRW 汇率源、税费模型、交易单位/最小变动价位校验或行业映射。

Market Light / 告警：

- Market Light 快照和 Market Light 告警仍只支持 `cn` / `hk` / `us`。
- Web 告警市场下拉不展示 `jp` / `kr`；后端 `normalize_market_region()` 对 `jp` / `kr` 返回显式 unsupported 错误。
- JP/KR 大盘复盘 v1 可生成报告和结构化 market review payload，但不等价于完整 Market Light 风控信号。
- 该轮边界收敛不改动 LLM Provider / Model / Base URL 的持久化语义，也不执行默认模型、运行时配置清理或回写；如需回滚，仅需恢复提交前 `.env` 与相关配置快照，并回退该功能提交。

回滚方式：移除 Portfolio snapshot 的 `data_quality` / `limitations` 扩展，并恢复告警前端/后端对市场枚举的旧边界说明。

## 审核核验与回退说明（Issue #1815）

- 兼容性核验：本次仅收敛 Market Light 告警到 `cn/hk/us`，不更改 provider/model/base_url 持久化链路；`.env.example`、`src/config.py`、`src/core/config_registry.py`、`src/services/system_config_service.py` 对已有 provider/provider-key 语义不做清空或迁移改写；可通过 `tests/test_system_config_service.py` 与 `tests/test_config_env_compat.py` 回归验证。
- 运行时回退：若需要恢复旧行为，移除 `jp/kr` 对 Market Light 的误用入口（或回退该 PR）即可，保留既有配置路径不变。
- Web UI 变更可追溯证据：`apps/dsa-web/src/components/alerts/__tests__/AlertRuleForm.test.tsx` 覆盖 `market` 场景下仅展示 `cn/hk/us`；如需页面级截图，可先执行
  `cd apps/dsa-web && npx playwright test --project=chromium --grep "market light"` 生成截图/trace；或在 PR 描述中用替代证据引用以下后端回归命令：
  - `python -m pytest tests/test_market_light_service.py tests/test_market_light_alerts.py -q`
  - `python -m pytest tests/test_portfolio_service.py -q`

## 台湾个股 suffix-only MVP（Issue #1772，Refs #1772）

当前阶段支持手动输入台湾股票的 Yahoo Finance 后缀代码，进入既有个股分析、历史保存和基础报告展示链路。TWSE 上市股票使用 `.TW` 后缀，TPEx 上柜（柜买）股票使用 `.TWO` 后缀，二者折叠为同一 `tw` 市场标签。**本次覆盖市场识别（detection）、数据路由层、DecisionSignal/Portfolio/Intelligence 服务层与 API 市场枚举，以及 DecisionSignal/Portfolio 前端市场类型与筛选**；台股股票索引/种子、Web 自动补全与告警（大盘红绿灯）市场放行仍作为后续 PR。对齐 #1718 日韩 MVP 模式。

支持格式：

- 上市（TWSE）：`2330.TW`、`0050.TW`
- 上柜（TPEx / 柜买）：`6488.TWO`、`5483.TWO`
- 代码 base 为 4-6 位数字（普通股 4 位，ETF/其他至 6 位，如 `00878.TW`、`006208.TW`），较日股 `.T` 的 4-5 位更宽。

约束与边界：

- **严格 suffix-only**：裸 `2330`、`00878` 等不带后缀的代码不会进入台股语义（`detect_market` / `get_market_for_stock` 仅在显式 `.TW`/`.TWO` 后缀时返回 `tw`）。本次**不引入任何台股股票索引/种子解析**，故裸码不可能经本地/远程股票池被改写为台股 suffix；该索引解析（与 jp/kr 同款的裸码命中行为）属后续 PR。
- 台股日线和基础实时/近实时行情只走 `YfinanceFetcher`，不尝试 AkShare、Tushare、Efinance、Pytdx、Baostock 等 A 股专属数据源。
- 基本面复用既有 offshore yfinance 轻量路径；A 股专属资金流、龙虎榜、板块等能力按 `not_supported` 降级。
- 报告 Prompt 已增加台股市场语义（新台币、三大法人、TWSE/TPEx ±10% 涨跌停），避免套用 A 股北向资金、龙虎榜等概念。
- 交易日历注册 `tw: XTAI / Asia/Taipei`。TWSE 为 09:00–13:30 连续交易、无午休；收盘集合竞价暂不建模，与 jp/kr 一致。若本地 `exchange-calendars` 版本缺少对应日历，既有 fail-open/fail-closed 语义保持不变。
- 主要指数提供加权指数 `^TWII` 与柜买指数 `^TWOII`。

不承诺项：

- 不承诺实时行情；Yahoo Finance 数据可能延迟或字段缺失。
- 不承诺完整基本面、行业/板块、市场宽度、涨跌家数或台股大盘复盘。
- 台股股票索引/种子、Web 自动补全与告警（大盘红绿灯）市场放行仍作为后续 PR；告警 MarketRegion 与后端 market_light 仍为 cn/hk/us，未含 tw。
- 不补齐 Portfolio 的 TWD 汇率、成本、市值完整口径（属上述后续 PR 范围）。

回滚方式：移除 `tw` 市场识别、交易日历注册、YFinance 路由扩展与服务层/API 市场枚举及前端市场类型放行，并删除本文档中的能力声明。
