# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

USB Assistant 是一个面向实体店商户的 AI 经营顾问平台。商户可上传销售数据、查询竞品、管理知识库，AI 以对话方式给出经营建议。支持多 LLM（Claude、OpenAI）和本地 Ollama。

## 启动与停止

```bash
# 启动所有服务（PostgreSQL + 后端 8081 + 前端 3001）
./start.command

# 停止所有服务
./stop.command

# 单独启动后端（开发调试用）
cd /Users/singhfang/usb-assistant
venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8081 --reload

# 单独启动前端静态服务
cd frontend && python3 -m http.server 3001
```

## 数据库连接

**绝对不要查 `usb_assistant.db`**，这个 SQLite 文件是历史遗留，服务从不使用它。

```bash
# 本地开发（Docker PostgreSQL，端口 5432）
docker exec usb-assistant-db-1 psql -U dev -d usb_assistant_dev -c "SELECT ..."

# 线上 Cloud SQL（需先启动 proxy，端口 5433）
PGPASSWORD=<密码> psql -h 127.0.0.1 -p 5433 -U postgres -d usb_assistant -c "SELECT ..."
```

服务实际连接哪个库由 `backend/.env` 里的 `DATABASE_URL` 决定。排查数据问题前先确认。

## 后端架构

入口 `backend/main.py`，FastAPI 应用，关键模块：

| 文件 | 职责 |
|------|------|
| `db.py` | SQLModel ORM，数据库引擎选择（PostgreSQL / SQLite fallback） |
| `auth.py` | JWT 双会话（用户 session + 独立 admin session） |
| `llm.py` | 多模型抽象，Claude / OpenAI / Ollama，含 SQL Agent（最多 6 轮） |
| `rag.py` | 双模式 RAG：`USE_PGVECTOR=true` → pgvector，否则 ChromaDB |
| `parser.py` | 多格式文件解析 → 标准化 JSON，Gemini / Ollama AI 兜底 |
| `prompts.py` | 系统提示词，analysis_card 格式定义 |
| `conversations.py` | 对话 CRUD |
| `admin.py` | 管理后台 API（用户、LLM 配置、平台知识库） |
| `kb.py` | 商户知识库文档管理 |
| `competitor.py` | Google Maps Places API 竞品查询 |
| `data_agent.py` | DuckDB SQL 执行（供 SQL Agent 调用） |
| `ingest.py` | CLI 批量导入平台知识库（markdown → pgvector） |
| `migrate_legacy.py` | 一次性迁移脚本（旧 JSON config → 数据库），正常情况不再运行 |

### RAG 模式切换

- `USE_PGVECTOR=true`（生产 / 本地开发）→ PostgreSQL pgvector，embedding 用 Gemini
- `USE_PGVECTOR=false` 或未设置 → ChromaDB 本地文件，embedding 用 Ollama `nomic-embed-text`
- embedding 维度 3072，两端必须一致，混用会导致查询失败

### Analysis Card 格式

LLM 输出中用 `<analysis_card>...</analysis_card>` 包裹 JSON，`main.py` 解析后推给前端：
- `type`: `diagnosis` | `opportunity` | `paradox` | `action`
- `severity`: `high` | `medium` | `low`
- `title` ≤ 80 字，`subtitle` ≤ 120 字
- `fields` 最多 4 个，`actions` 最多 5 个
- JSON 值内不能出现 ASCII 双引号（会导致解析失败）

## 已知历史遗留问题

### 路径 / 模块
- `migrate_legacy.py` 从 `db` 导入 `DB_PATH`，但 `DB_PATH` 只在 `DATABASE_URL` 未设置时定义，PostgreSQL 环境下运行此脚本会报 `ImportError`。该脚本已完成历史使命，不应再运行。
- `ingest.py` 和 `rag.py` 中 ChromaDB 路径硬编码为 `~/usb-assistant/chromadb`，`USE_PGVECTOR=true` 时不生效但代码仍存在。
- `kb.py` 注释里仍有 "ChromaDB-only sources" 逻辑，实际 pgvector 环境下这些分支不走。

### 数据库
- `db.py` SQLite fallback 路径 `~/usb-assistant/usb_assistant.db`，正式环境永远不用，但代码保留。
- 没有 Alembic 迁移管理，schema 变更靠 `SQLModel.metadata.create_all()`（只能加表不能改列）。
- `delete_conversation` 之前未加 `db.flush()`，PostgreSQL 外键约束下会报 `ForeignKeyViolation`（已修复，2026-04-28）。

### 模型 / 配置
- `auth.py` JWT_SECRET 默认值 `"dev-secret-change-in-production"`，未设环境变量时生产环境极不安全。
- `llm.py` Ollama generate URL `http://localhost:11434/api/generate` 硬编码，无环境变量覆盖。
- `rag.py` Ollama embed URL `http://localhost:11434/api/embeddings` 同上。
- `parser.py` Ollama URL 同上（第三处硬编码）。
- `main.py` CORS 只允许 `localhost:3001`，部署到其他域名需要手动改。
- SQL Agent 最多 6 轮，复杂分析任务可能被截断。

### 前端
- `index.html` 内 `API = 'http://localhost:8081'`，hostname 非 localhost 时回退空字符串（走相对路径），与 CORS 配置耦合。

## 环境变量（backend/.env）

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | PostgreSQL 连接串（asyncpg 格式），未设则用 SQLite |
| `USE_PGVECTOR` | `true` 启用 pgvector RAG，否则用 ChromaDB |
| `ANTHROPIC_API_KEY` | Claude API |
| `OPENAI_API_KEY` | OpenAI API |
| `GEMINI_API_KEY` | Gemini embedding（RAG 用） |
| `GOOGLE_MAPS_KEY` | 竞品查询 |
| `JWT_SECRET` | 必须设置，否则用不安全默认值 |
| `OLLAMA_BASE_URL` | Ollama 服务地址，默认 `http://localhost:11434` |
| `ALLOWED_ORIGINS` | 逗号分隔的允许跨域来源，默认 `http://localhost:3001,http://127.0.0.1:3001` |

## 服务端口

| 服务 | 端口 |
|------|------|
| 前端静态 | 3001 |
| 后端 API | 8081 |
| PostgreSQL（本地 Docker） | 5432 |
| PostgreSQL（Cloud SQL Proxy） | 5433 |
| Ollama | 11434 |
