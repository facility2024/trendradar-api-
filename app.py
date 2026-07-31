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

app = Flask(__name__)
# Libera chamadas de outros domínios (ex: seu painel hospedado no Lovable)
CORS(app, resources={r"/api/*": {"origins": "*"}})
tr = Trends()

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
        # Evolução do interesse ao longo do tempo
        iot_df = tr.interest_over_time(keyword, geo=geo, timeframe=timeframe)
        interest_over_time = _series_to_list(iot_df, keyword)

        # Ranking de cidades mais interessadas nesse termo
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

        # Buscas relacionadas em alta (bônus, se disponível)
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
