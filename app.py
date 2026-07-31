"""
TrendRadar — painel de tendências de busca (Google Trends) por palavra-chave,
com evolução no tempo e ranking de cidades mais interessadas.

Fonte de dados: trendspy (biblioteca não-oficial, sucessora do pytrends,
que está arquivado desde abril/2025).
"""

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from trendspy import Trends
import traceback
import time
import os
import requests

app = Flask(__name__)
# Libera chamadas de outros domínios (ex: seu painel hospedado no Lovable)
CORS(app, resources={r"/api/*": {"origins": "*"}})
tr = Trends()

# Chave da API do FastMoss (produtos virais do TikTok Shop).
# Configurada como variável de ambiente no Render, nunca escrita direto no código.
FASTMOSS_API_KEY = os.environ.get("FASTMOSS_API_KEY", "")
FASTMOSS_BASE_URL = "https://openapi.fastmoss.com"

# Cache simples em memória: guarda o resultado de cada busca por um tempo,
# assim buscas repetidas do mesmo termo não batem no Google de novo
# (reduz bastante o risco de bloqueio por excesso de requisições).
_CACHE = {}
_CACHE_TTL_SECONDS = 20 * 60  # 20 minutos

# Janelas de tempo que o front-end pode pedir (máximo 5 dias, como pedido)
TIMEFRAMES = {
    "day": "now 1-d",
    "5d": "now 5-d",
}


def _series_to_list(df, keyword):
    """Converte a coluna do keyword num DataFrame de interesse-no-tempo em uma lista de pontos."""
    if df is None or df.empty:
        return []
    col = keyword if keyword in df.columns else df.columns[0]
    points = []
    for ts, row in df.iterrows():
        points.append({
            "date": ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts),
            "value": int(row[col]) if row[col] == row[col] else 0,  # trata NaN
        })
    return points


def _region_to_list(df, keyword, limit=15):
    """Converte o DataFrame de interesse-por-região num ranking ordenado de cidades."""
    if df is None or df.empty:
        return []
    col = keyword if keyword in df.columns else df.columns[0]
    ranked = df[[col]].sort_values(by=col, ascending=False)
    out = []
    for city, row in ranked.head(limit).iterrows():
        val = row[col]
        out.append({"city": str(city), "value": int(val) if val == val else 0})
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/trends")
def api_trends():
    keyword = (request.args.get("keyword") or "").strip()
    period = request.args.get("period", "5d")
    geo = request.args.get("geo", "BR")

    if not keyword:
        return jsonify({"error": "Informe uma palavra-chave."}), 400
    if period not in TIMEFRAMES:
        period = "5d"

    timeframe = TIMEFRAMES[period]

    cache_key = f"{keyword.lower()}|{period}|{geo}"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL_SECONDS:
        return jsonify(cached["data"])

    try:
        iot_df = tr.interest_over_time(keyword, geo=geo, timeframe=timeframe)
        interest_over_time = _series_to_list(iot_df, keyword)

        region_df = None
        for tf in [timeframe, "today 5-d", "today 1-m"]:
            for resolution in ["CITY", "REGION"]:
                try:
                    region_df = tr.interest_by_region(keyword, geo=geo, resolution=resolution, timeframe=tf)
                    if region_df is not None and not region_df.empty:
                        break
                except Exception:
                    region_df = None
            if region_df is not None and not region_df.empty:
                break
        top_cities = _region_to_list(region_df, keyword)

        rising = []
        try:
            related = tr.related_queries(keyword, geo=geo, timeframe=timeframe)
            rising_df = related.get("rising") if isinstance(related, dict) else None
            if rising_df is not None and not rising_df.empty:
                for _, row in rising_df.head(8).iterrows():
                    rising.append({
                        "query": str(row.get("query", "")),
                        "value": str(row.get("value", "")),
                    })
        except Exception:
            pass

        result = {
            "keyword": keyword,
            "geo": geo,
            "period": period,
            "interest_over_time": interest_over_time,
            "top_cities": top_cities,
            "rising_queries": rising,
        }
        _CACHE[cache_key] = {"ts": time.time(), "data": result}
        return jsonify(result)

    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": f"Não foi possível consultar o Google Trends agora: {exc}"}), 502


@app.route("/api/tiktok-trending")
def tiktok_trending():
    """Produtos mais vendidos/virais do TikTok Shop, via FastMoss OpenAPI."""
    if not FASTMOSS_API_KEY:
        return jsonify({"error": "FASTMOSS_API_KEY não configurada no servidor."}), 500

    region = (request.args.get("region") or "BR").upper()
    limit = min(int(request.args.get("limit", 10)), 20)

    cache_key = f"tiktok|{region}|{limit}"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL_SECONDS:
        return jsonify(cached["data"])

    try:
        resp = requests.post(
            f"{FASTMOSS_BASE_URL}/product/v1/rank/topSelling",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {FASTMOSS_API_KEY}",
            },
            json={
                "filter": {"region": region},
                "orderby": [{"field": "gmv", "order": "desc"}],
                "page": 1,
                "pagesize": limit,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("code") != 0:
            return jsonify({"error": payload.get("message", "Erro retornado pelo FastMoss."), "raw": payload}), 502

        items = payload.get("data", {}).get("list", [])
        products = [{
            "id": item.get("product_id"),
            "title": item.get("title"),
            "price": item.get("real_price"),
            "units_sold": item.get("units_sold"),
            "gmv": item.get("gmv"),
            "growth_rate": item.get("growth_rate"),
        } for item in items]

        result = {"region": region, "products": products}
        _CACHE[cache_key] = {"ts": time.time(), "data": result}
        return jsonify(result)

    except requests.exceptions.HTTPError as exc:
        traceback.print_exc()
        return jsonify({"error": f"FastMoss recusou a requisição: {exc}", "detail": resp.text[:500]}), 502
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": f"Não foi possível consultar o FastMoss agora: {exc}"}), 502


if __name__ == "__main__":
    app.run(debug=True, port=5000)
