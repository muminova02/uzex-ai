"""SQL allowlist validator: one of several layers, not complete security by itself.

Layers (all always on):
1. This module: a single SELECT/UNION statement; every AST node type, function, physical table
   and column must be on an allowlist; CTE names are resolved with lexical scope; dynamic table
   functions, file paths, system catalogs, qualified names, named placeholders, recursive CTEs,
   and statements such as PRAGMA/SET/ATTACH/COPY are rejected. A token-level keyword check runs
   in addition, because AST parsing alone can miss parser fallbacks (``Command``).
2. executor.py: the DB is opened read-only with ``enable_external_access=false``, extension
   autoload/autoinstall disabled and ``lock_configuration=true``, in a separate worker process
   with memory/thread limits and a hard kill on timeout.
3. Values are always bound as parameters (``?``), never formatted into SQL.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

MAX_SQL_CHARS = 12000
MAX_NODES = 3000

ALLOWED_NODES = {
    exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With, exp.CTE, exp.Subquery, exp.From, exp.Join,
    exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit, exp.Offset, exp.Column,
    exp.Identifier, exp.Table, exp.TableAlias, exp.Alias, exp.Literal, exp.Star, exp.Paren, exp.And, exp.Or,
    exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Is, exp.Null,
    exp.Boolean, exp.Like, exp.ILike, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.IntDiv, exp.Mod, exp.Neg,
    exp.Case, exp.If, exp.Cast, exp.TryCast, exp.DataType, exp.DataTypeParam, exp.Window, exp.Distinct,
    exp.Placeholder, exp.Filter, exp.Exists, exp.Tuple, exp.Var, exp.DPipe, exp.Interval, exp.Nullif,
}
ALLOWED_FUNCS = {
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.CountIf, exp.Median, exp.Coalesce, exp.Round,
    exp.Abs, exp.Lower, exp.Upper, exp.Year, exp.Month, exp.Quarter, exp.Day, exp.Extract, exp.Greatest,
    exp.Least, exp.Length, exp.Trim, exp.Concat, exp.TimestampTrunc, exp.DateTrunc, exp.Floor, exp.Ceil,
    exp.RowNumber, exp.Rank, exp.DenseRank, exp.Stddev, exp.ApproxDistinct,
}
ALLOWED_ANON_FUNCS = {"strip_accents"}
DENY_KEYWORDS = {
    "ATTACH", "DETACH", "INSTALL", "LOAD", "PRAGMA", "SET", "RESET", "COPY", "EXPORT", "IMPORT", "CALL",
    "CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE", "GRANT", "REVOKE",
    "VACUUM", "CHECKPOINT", "USE", "BEGIN", "COMMIT", "ROLLBACK", "DESCRIBE", "SHOW", "SUMMARIZE",
    "EXPLAIN", "ANALYZE", "RECURSIVE", "LATERAL", "PIVOT", "UNPIVOT", "TABLESAMPLE", "QUALIFY",
}
DENY_NAME_PREFIXES = ("duckdb_", "pg_", "sqlite_", "information_schema", "read_", "glob", "parquet_", "json_")


class SQLRejected(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass
class GuardResult:
    sql: str
    tables: set[str]
    output_columns: list[str]
    placeholders: int


class SQLGuard:
    def __init__(self, schema: dict[str, set[str]]):
        """schema: allowlisted physical table -> allowlisted column names (lower case)."""
        self.schema = {t.lower(): {c.lower() for c in cols} for t, cols in schema.items()}

    # ------------------------------------------------------------------ public
    def validate(self, sql: str, params: list | tuple = ()) -> GuardResult:
        if not isinstance(sql, str) or not sql.strip():
            raise SQLRejected("empty")
        if len(sql) > MAX_SQL_CHARS:
            raise SQLRejected("too_long", f"{len(sql)} chars")
        self._check_tokens(sql)
        try:
            statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
        except (ParseError, TokenError) as e:
            raise SQLRejected("parse_error", str(e).splitlines()[0][:300]) from e
        if len(statements) != 1:
            raise SQLRejected("statement_count", f"exactly one statement allowed, got {len(statements)}")
        root = statements[0]
        if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise SQLRejected("not_select", type(root).__name__)
        nodes = list(root.walk())
        if len(nodes) > MAX_NODES:
            raise SQLRejected("too_complex", f"{len(nodes)} nodes")
        for node in nodes:
            self._check_node(node)
        placeholders = [n for n in nodes if isinstance(n, exp.Placeholder)]
        if len(placeholders) != len(params):
            raise SQLRejected("param_count", f"{len(placeholders)} placeholders, {len(params)} params")
        for p in params:
            if p is not None and not isinstance(p, (str, int, float, Decimal, dt.date, bool)):
                raise SQLRejected("param_type", type(p).__name__)
        tables: set[str] = set()
        out = self._check_query(root, {}, {}, tables)
        return GuardResult(sql, tables, out, len(placeholders))

    # ------------------------------------------------------------------ tokens
    def _check_tokens(self, sql: str) -> None:
        try:
            toks = sqlglot.tokenize(sql, read="duckdb")
        except (TokenError, ParseError) as e:
            raise SQLRejected("parse_error", str(e)[:300]) from e
        for t in toks:
            if t.token_type in (TokenType.STRING, TokenType.NATIONAL_STRING):
                continue
            word = t.text.upper()
            if word in DENY_KEYWORDS:
                raise SQLRejected("forbidden_keyword", word)
            if t.token_type in (TokenType.VAR, TokenType.IDENTIFIER) and t.text.lower().startswith(DENY_NAME_PREFIXES):
                raise SQLRejected("forbidden_name", t.text)
            if t.token_type == TokenType.SEMICOLON and t is not toks[-1]:
                raise SQLRejected("statement_count", "semicolon inside statement")

    # ------------------------------------------------------------------ nodes
    def _check_node(self, node: exp.Expression) -> None:
        cls = type(node)
        if isinstance(node, exp.Func) and cls not in ALLOWED_NODES:  # (sqlglot models And/Or/... as Func too)
            if isinstance(node, exp.Anonymous):
                name = str(node.this).lower()
                if name not in ALLOWED_ANON_FUNCS:
                    raise SQLRejected("function_not_allowed", name)
                return
            if cls not in ALLOWED_FUNCS:
                raise SQLRejected("function_not_allowed", cls.__name__)
            return
        if cls not in ALLOWED_NODES:
            raise SQLRejected("construct_not_allowed", cls.__name__)
        if isinstance(node, exp.Placeholder) and node.this not in (None, "?"):
            raise SQLRejected("named_placeholder", str(node.this))
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLRejected("recursive_cte")
        if isinstance(node, exp.Table):
            if node.args.get("db") or node.args.get("catalog"):
                raise SQLRejected("qualified_table", node.sql("duckdb"))
            if not isinstance(node.this, exp.Identifier):
                raise SQLRejected("table_function", node.sql("duckdb")[:80])
        if isinstance(node, exp.Column) and (node.args.get("db") or node.args.get("catalog")):
            raise SQLRejected("qualified_column", node.sql("duckdb"))

    # ------------------------------------------------------------------ scopes
    def _check_query(self, q: exp.Expression, ctes: dict[str, set[str]], outer: dict[str, set[str]],
                     tables: set[str]) -> list[str]:
        if isinstance(q, exp.Subquery):
            return self._check_query(q.this, ctes, outer, tables)
        if isinstance(q, (exp.Union, exp.Intersect, exp.Except)):
            local = self._with_ctes(q, ctes, outer, tables)
            left = self._check_query(q.left, local, outer, tables)
            right = self._check_query(q.right, local, outer, tables)
            if len(left) != len(right) and "*" not in left + right:
                raise SQLRejected("set_operation_arity", f"{len(left)} vs {len(right)}")
            return left
        if not isinstance(q, exp.Select):
            raise SQLRejected("construct_not_allowed", type(q).__name__)
        local = self._with_ctes(q, ctes, outer, tables)

        sources: dict[str, set[str]] = {}
        from_ = q.args.get("from_") or q.args.get("from")
        src_exprs = ([from_.this] if from_ else []) + [j.this for j in q.args.get("joins") or []]
        for src in src_exprs:
            alias, cols = self._source(src, local, outer, tables)
            if alias in sources:
                raise SQLRejected("duplicate_alias", alias)
            sources[alias] = cols

        visible = {**outer, **sources}
        select_aliases = {p.alias.lower() for p in q.expressions if isinstance(p, exp.Alias)}
        for node in self._own_nodes(q):
            if isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                self._check_query(node, local, visible, tables)
            elif isinstance(node, exp.Column):
                self._check_column(node, sources, outer, select_aliases)

        out: list[str] = []
        for p in q.expressions:
            if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)):
                for cols in sources.values():
                    out.extend(sorted(cols))
            else:
                out.append(p.alias_or_name.lower())
        return out

    def _with_ctes(self, q, ctes, outer, tables):
        local = dict(ctes)
        w = q.args.get("with_") or q.args.get("with")
        if w:
            for cte in w.expressions:
                name = cte.alias.lower()
                if not name or name in self.schema or name in local:
                    raise SQLRejected("cte_name_not_allowed", name or "<empty>")
                cols = self._check_query(cte.this, local, outer, tables)
                alias_cols = [c.name.lower() for c in (cte.args.get("alias").columns if cte.args.get("alias") else [])]
                local[name] = set(alias_cols or cols)
        return local

    def _source(self, src, ctes, outer, tables) -> tuple[str, set[str]]:
        if isinstance(src, exp.Table):
            name = src.name.lower()
            alias = (src.alias or src.name).lower()
            if name in ctes:
                return alias, ctes[name]
            if name not in self.schema:
                raise SQLRejected("table_not_allowed", src.name)
            tables.add(name)
            return alias, self.schema[name]
        if isinstance(src, exp.Subquery):
            if not src.alias:
                raise SQLRejected("subquery_alias_required")
            return src.alias.lower(), set(self._check_query(src.this, ctes, outer, tables))
        raise SQLRejected("source_not_allowed", type(src).__name__)

    @staticmethod
    def _own_nodes(q: exp.Select):
        """Nodes of this SELECT without descending into nested queries (yielded, not entered)."""
        stack = [v for k, v in q.args.items() if k not in ("with_", "with", "from_", "from")]
        from_ = q.args.get("from_") or q.args.get("from")
        for j in q.args.get("joins") or []:
            stack.append(j.args.get("on"))
            stack.append(j.args.get("using"))
        if from_ is not None and isinstance(from_.this, exp.Subquery):
            pass  # checked as a source
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, exp.Expression):
                continue
            if isinstance(item, exp.Join):
                continue
            if isinstance(item, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                yield item
                continue
            yield item
            for v in item.args.values():
                stack.append(v)

    def _check_column(self, col: exp.Column, sources, outer, select_aliases) -> None:
        if isinstance(col.this, exp.Star):
            if col.table and col.table.lower() not in sources:
                raise SQLRejected("unknown_table_alias", col.table)
            return
        name = col.name.lower()
        if col.table:
            t = col.table.lower()
            cols = sources.get(t, outer.get(t))
            if cols is None:
                raise SQLRejected("unknown_table_alias", col.table)
            if name not in cols:
                raise SQLRejected("unknown_column", f"{col.table}.{col.name}")
            return
        if any(name in cols for cols in sources.values()) or name in select_aliases:
            return
        if any(name in cols for cols in outer.values()):
            return
        raise SQLRejected("unknown_column", col.name)
