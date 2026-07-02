# 项目地图（PROJECT_MAP.md）

> 一句话看懂每个目录/关键文件是干什么的。本项目是"股票智能分析系统"（A股/港股/美股）：
> 抓数据 → 技术分析/新闻检索 → AI 分析 → 生成报告 → 推送通知。

## 根目录关键文件

- `main.py` → 分析任务主入口（命令行手动跑分析用这个）
- `server.py` → Web/API 服务入口（网页版后台）
- `webui.py` → 旧版 Web UI 入口
- `requirements.txt` / `pyproject.toml` → Python 依赖清单
- `AGENTS.md` → 仓库 AI 协作开发规则（`CLAUDE.md` 是它的软链接）
- `README.md` → 项目定位、核心能力、快速开始

## 主要目录

- `strategies/` → **交易策略目录**：每个 YAML 文件是一条用自然语言写的分析策略，系统启动自动加载
  - `a_share_composite.yaml` → A股复合策略（2026调研版）：市场环境→主线板块→哑铃选股→A股风控四层框架
  - `bull_trend.yaml` → 默认多头趋势策略（当前默认激活）
  - 其余 14 个 → 龙头、情绪周期、缩量回踩、放量突破、缠论、波浪等单一视角策略
- `src/` → 后端核心逻辑
  - `src/core/` → 主流程编排（分析流水线、大盘复盘、回测引擎、交易日历、配置）
  - `src/agent/` → 多智能体分析（技术面/情报/风险/决策 agent、策略技能加载与路由、工具集）
  - `src/services/` → 业务服务层（分析、组合、告警、决策信号、选股 AlphaSift 桥接等）
  - `swing_picks_service.py` → 短线荐股：每日从全A股筛选+AI精选N只2-3天短线候选，管理持仓状态
  - `swing_picks_worker.py` → 短线荐股后台监控：盘前触发荐股、盘中到价/到期推送卖出提醒
  - `src/repositories/` → 数据库读写层
  - `src/llm/` → 大模型调用封装
  - `src/reports/`、`src/schemas/` → 报告生成与数据结构定义
- `data_provider/` → 行情数据源适配（Tushare、AkShare、Efinance、腾讯、新浪等，带故障切换）
- `api/` → FastAPI 接口层（网页端调用的 REST API）
- `bot/` → 机器人接入（钉钉、飞书、Discord 的指令处理）
- `apps/dsa-web/` → Web 前端页面
- `apps/dsa-desktop/` → Electron 桌面客户端
- `scripts/` → 运维/辅助脚本（含 `deploy_vps.sh` 一键部署）
- `docker/` → Docker 部署配置
- `docs/` → 项目文档（部署、FAQ、配置指南、CHANGELOG 等）
- `tests/` → 自动化测试
- `data/`、`logs/`、`reports/` → 运行期产生的数据、日志、报告（非源码）
- `static/`、`templates/` → 静态资源与报告模板
