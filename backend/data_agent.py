"""
Data Agent — DuckDB-backed SQL execution for uploaded sales files.

Public API:
  load_to_duckdb(file_paths, conn=None) -> (conn, schema_text)
  execute_sql(conn, sql) -> str
  SQL_TOOL_DEFINITION — Claude tool definition for execute_sql
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd


# ── Claude tool definition ────────────────────────────────────────────────────

SQL_TOOL_DEFINITION: dict = {
    "name": "execute_sql",
    "description": (
        "对已上传的销售数据执行 SQL 查询（DuckDB 方言）。"
        "可多次调用，每次查询不同维度。"
        "表名和列名见系统提示中的 schema 描述。"
        "支持 GROUP BY、WINDOW 函数、日期函数等标准 SQL。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "要执行的 SQL 语句，使用 DuckDB 方言",
            },
            "reason": {
                "type": "string",
                "description": "这条查询的目的（一句话），用于向用户展示进度",
            },
        },
        "required": ["sql", "reason"],
    },
}


# ── Load files into DuckDB ────────────────────────────────────────────────────

def load_to_duckdb(
    file_paths: list[Path],
    conn: duckdb.DuckDBPyConnection | None = None,
) -> tuple[duckdb.DuckDBPyConnection, str]:
    """
    Load one or more Excel/CSV files into an in-memory DuckDB connection.

    If conn is provided (existing session), new tables are added to it.
    Returns (conn, schema_description_for_llm).
    """
    if conn is None:
        conn = duckdb.connect()

    schema_parts: list[str] = []

    for path in file_paths:
        path = Path(path)
        table_name = _safe_table_name(path.stem)

        # Avoid duplicate table names by appending a suffix
        existing = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        base = table_name
        i = 2
        while table_name in existing:
            table_name = f"{base}_{i}"
            i += 1

        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        elif suffix == ".csv":
            df = pd.read_csv(path)
        else:
            continue

        conn.register(table_name, df)
        schema_parts.append(_describe_table(table_name, df))

    return conn, "\n\n".join(schema_parts)


def _safe_table_name(stem: str) -> str:
    """Convert a file stem to a safe SQL table name."""
    name = stem.lower()
    for ch in (" ", "-", ".", "(", ")"):
        name = name.replace(ch, "_")
    # Strip leading digits
    if name and name[0].isdigit():
        name = "t_" + name
    return name


def _describe_table(table_name: str, df: pd.DataFrame) -> str:
    """Generate a schema description string for the LLM."""
    col_lines: list[str] = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        samples = df[col].dropna().head(3).tolist()
        samples_str = ", ".join(repr(v) for v in samples)
        col_lines.append(f"  - {col} ({dtype}): 示例值 [{samples_str}]")

    return (
        f"表名：{table_name}\n"
        f"行数：{len(df)}\n"
        f"列：\n" + "\n".join(col_lines)
    )


# ── Execute SQL ───────────────────────────────────────────────────────────────

MAX_RESULT_ROWS = 50


def execute_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    """
    Execute a SQL query and return a human-readable string result.
    Truncates to MAX_RESULT_ROWS rows to keep LLM context manageable.
    """
    try:
        result_df = conn.execute(sql).fetchdf()
    except Exception as e:
        return f"SQL 执行错误：{e}"

    if result_df.empty:
        return "查询结果为空（0 行）"

    total = len(result_df)
    truncated = result_df.head(MAX_RESULT_ROWS)
    text = truncated.to_string(index=False)

    if total > MAX_RESULT_ROWS:
        text += f"\n\n（结果共 {total} 行，已截断显示前 {MAX_RESULT_ROWS} 行）"
    else:
        text += f"\n\n（共 {total} 行）"

    return text
