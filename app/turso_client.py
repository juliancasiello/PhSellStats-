import httpx
from typing import Any, Iterable, Optional, Sequence


def _encode_arg(val: Any) -> dict[str, Any]:
    if val is None:
        return {"type": "null"}
    if isinstance(val, bool):
        return {"type": "integer", "value": "1" if val else "0"}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    if isinstance(val, float):
        return {"type": "float", "value": val}
    return {"type": "text", "value": str(val)}


def _decode_cell(cell: dict[str, Any]) -> Any:
    ctype = cell.get("type")
    if ctype == "null":
        return None
    if ctype == "integer":
        return int(cell["value"])
    if ctype == "float":
        return float(cell["value"])
    return cell.get("value")


class TursoCursor:
    def __init__(self, cols: list[str], rows: list[list[Any]]) -> None:
        self._rows: list[dict[str, Any]] = [
            {cols[i]: row[i] for i in range(len(cols))} for row in rows
        ]
        self._idx = 0

    def fetchone(self) -> Optional[dict[str, Any]]:
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self) -> list[dict[str, Any]]:
        res = self._rows[self._idx :]
        self._idx = len(self._rows)
        return res


class TursoConnection:
    """Drop-in sqlite3.Connection-compatible wrapper over Turso's /v2/pipeline HTTP API."""

    def __init__(self, database_url: str, auth_token: str) -> None:
        http_url = database_url.strip()
        if http_url.startswith("libsql://"):
            http_url = "https://" + http_url[len("libsql://") :]
        self._endpoint = http_url.rstrip("/") + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {auth_token.strip()}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(timeout=30.0)

    def _run_pipeline(self, stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        requests = [{"type": "execute", "stmt": s} for s in stmts]
        requests.append({"type": "close"})
        resp = self._client.post(self._endpoint, headers=self._headers, json={"requests": requests})
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", []):
            if item.get("type") == "error":
                err = item.get("error", {})
                raise RuntimeError(f"Turso SQL error: {err.get('message', err)}")
            res_obj = item.get("response", {})
            if res_obj.get("type") == "execute":
                results.append(res_obj["result"])
        return results

    def execute(self, sql: str, params: Sequence[Any] = ()) -> TursoCursor:
        stmt: dict[str, Any] = {"sql": sql}
        if params:
            stmt["args"] = [_encode_arg(p) for p in params]
        results = self._run_pipeline([stmt])
        if not results:
            return TursoCursor([], [])
        res = results[0]
        cols = [c["name"] for c in res.get("cols", [])]
        rows = [[_decode_cell(cell) for cell in row] for row in res.get("rows", [])]
        return TursoCursor(cols, rows)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        stmts = []
        for params in seq_of_params:
            stmt: dict[str, Any] = {"sql": sql}
            if params:
                stmt["args"] = [_encode_arg(p) for p in params]
            stmts.append(stmt)
        if not stmts:
            return
        # Batch in chunks of 100 statements per HTTP call
        for i in range(0, len(stmts), 100):
            self._run_pipeline(stmts[i : i + 100])

    def executescript(self, script: str) -> None:
        stmts = []
        for raw in script.split(";"):
            s = raw.strip()
            if s:
                stmts.append({"sql": s})
        if stmts:
            self._run_pipeline(stmts)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self._client.close()
