"""Small, read-only client for verified public Delta Exchange India endpoints."""
from __future__ import annotations
import json, time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .config import API_BASE

class DeltaPublicClient:
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(API_BASE + path + query, headers={"Accept": "application/json"})
        last: Exception | None = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=12) as response:
                    payload = json.load(response)
                if not payload.get("success", False):
                    raise RuntimeError(f"Delta unsuccessful response: {payload}")
                return payload
            except Exception as exc:
                last = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"Delta request failed: {path}") from last

    def products(self, states: str | None = None) -> list[dict[str, Any]]:
        return self.get("/products", {"states": states} if states else None)["result"]

    def expired_btc_options(self) -> list[dict[str, Any]]:
        """Fetch all publicly reachable expired BTC calls/puts using Delta cursor pagination."""
        params: dict[str, Any] = {"states":"expired", "contract_types":"call_options,put_options", "page_size":3000}
        all_products: list[dict[str, Any]]=[]
        while True:
            payload=self.get('/products',params)
            # Filter and slim before retaining: raw full products are very large nested objects.
            for p in payload['result']:
                if (p.get('underlying_asset') or {}).get('symbol') == 'BTC':
                    all_products.append({k:p.get(k) for k in ('symbol','contract_type','launch_time','settlement_time','strike_price','contract_value','taker_commission_rate','settlement_price','id')})
            cursor=(payload.get('meta') or {}).get('after')
            if not cursor: break
            params['after']=cursor
        return all_products

    def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[dict[str, Any]]:
        return self.get("/history/candles", {"symbol": symbol, "resolution": resolution, "start": start, "end": end})["result"]

    def option_chain(self, expiry: date | None = None, underlying: str = "BTC") -> list[dict[str, Any]]:
        params: dict[str, str] = {"contract_types": "call_options,put_options", "underlying_asset_symbols": underlying}
        if expiry:
            params["expiry_date"] = expiry.strftime("%d-%m-%Y")
        return self.get("/tickers", params)["result"]

def save_api_evidence(path: Path, endpoint: str, params: dict[str, Any], payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"downloaded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "endpoint": endpoint, "params": params, "response": payload}, indent=2), encoding="utf-8")
