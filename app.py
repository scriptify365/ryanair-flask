from flask import Flask, request, render_template, Response, abort
import requests
from datetime import datetime, timedelta, date
import math
import os, time, logging
from logging.handlers import RotatingFileHandler

# --- Optional deps (safe imports) ---
try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    _SENTRY_OK = True
except Exception:
    _SENTRY_OK = False

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _PROM_OK = True
except Exception:
    _PROM_OK = False
    # minimal placeholders to avoid NameError if someone hits /metrics accidentally
    Counter = Histogram = lambda *a, **k: None
    CONTENT_TYPE_LATEST = "text/plain"



app = Flask(__name__, template_folder="templates", static_folder="static")

# ======================
# Monitoring bootstrap
# ======================
@app.context_processor
def inject_brand():
    # Change brand via env without touching templates
    return {"brand": os.getenv("APP_BRAND", "Flynair")}

# Sentry (only if DSN set and package available)
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_OK and SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES", "0.2")),   # 20% sampling
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES", "0.0"))
    )

# Prometheus counters/histograms
if _PROM_OK:
    REQUEST_COUNT = Counter(
        "http_requests_total", "Total HTTP requests",
        ["method", "endpoint", "status"]
    )
    REQUEST_LATENCY = Histogram(
        "http_request_duration_seconds", "Request latency (seconds)",
        ["endpoint"],
        buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5)
    )

# Rotating logs
os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler("logs/app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
handler.setFormatter(logging.Formatter(
    '%(asctime)s level=%(levelname)s module=%(module)s path="%(pathname)s" '
    'line=%(lineno)d msg="%(message)s"'
))
handler.setLevel(logging.INFO)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

@app.before_request
def _start_timer():
    request._start_time = time.perf_counter()

@app.after_request
def _record_metrics(response):
    # safe, lightweight metrics + access log
    try:
        endpoint = request.endpoint or "unknown"
        dur = None
        if hasattr(request, "_start_time"):
            dur = time.perf_counter() - request._start_time
            if _PROM_OK:
                REQUEST_LATENCY.labels(endpoint=endpoint).observe(dur)
        if _PROM_OK:
            REQUEST_COUNT.labels(method=request.method, endpoint=endpoint, status=response.status_code).inc()
        app.logger.info(f'{request.method} {request.path} {response.status_code} dur={dur:.3f}s')
    except Exception as e:
        app.logger.warning(f"metrics failed: {e}")
    return response

@app.route("/metrics")
def metrics():
    """
    Prometheus metrics (protected by bearer token if METRICS_TOKEN is set).
    Header: Authorization: Bearer <token>
    """
    if not _PROM_OK:
        return Response("prometheus-client not installed", status=503, mimetype="text/plain")
    token = os.getenv("METRICS_TOKEN")
    if token:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return abort(401)
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

# ======================
# Ryanair data + helpers
# ======================

def fetch_airports_data():
    ua = {"User-Agent": "Mozilla/5.0"}
    url = "https://www.ryanair.com/api/views/locate/5/airports/pl/active"
    try:
        r = requests.get(url, headers=ua, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[airports] {e}")
        data = []

    airports_raw, countries_set = [], {}
    for item in data:
        code = item.get("code")
        country = (item.get("country") or {})
        country_code = country.get("code") or ""
        country_name = country.get("name") or ""
        city = (item.get("city") or {}).get("name") or ""
        name = item.get("name") or ""

        if not name or name == city:
            label = f"{city} ({code})" if city else f"{code}"
        else:
            label = f"{city} – {name} ({code})" if city and city not in name else f"{name} ({code})"

        airports_raw.append({
            "code": code, "label": label,
            "country_code": country_code, "country_name": country_name
        })
        if country_code:
            countries_set[country_code] = country_name or country_code

    airports_for_select = sorted([(a["code"], a["label"]) for a in airports_raw], key=lambda x: x[1])
    countries_for_select = sorted([(c, n) for c, n in countries_set.items()], key=lambda x: x[1])
    return airports_raw, airports_for_select, countries_for_select

AIRPORTS_RAW, AIRPORTS, COUNTRIES = fetch_airports_data()
MARKET_BY_CURRENCY = {"EUR": "en-ie", "PLN": "pl-pl", "GBP": "en-gb"}

def build_ryanair_link(one_way, dep, arr, out_date, in_date=None):
    base = "https://www.ryanair.com/gb/en/trip/flights/select"
    params = {
        "adults": "1","teens": "0","children": "0","infants": "0",
        "originIata": dep, "destinationIata": arr, "dateOut": out_date,
        "isConnectedFlight": "false","reserveSeats": "false",
    }
    if not one_way and in_date:
        params["dateIn"] = in_date
    q = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{base}?{q}"

def parse_iso_date(d):
    if not d: return None
    return datetime.strptime(d.split("T")[0], "%Y-%m-%d").date()

@app.template_filter("weekday_name")
def weekday_name(date_str):
    try:
        dt = datetime.strptime(date_str.split("T")[0], "%Y-%m-%d")
        return dt.strftime("%A")
    except Exception:
        return ""

# simple FX
FX = {
    ("EUR", "PLN"): 4.30, ("PLN", "EUR"): 1/4.30,
    ("EUR", "GBP"): 0.86, ("GBP", "EUR"): 1/0.86,
    ("PLN", "GBP"): (1/4.30)*0.86, ("GBP", "PLN"): (1/0.86)*4.30,
}
def convert_price(amount, from_cur, to_cur):
    if from_cur == to_cur: return float(amount)
    rate = FX.get((from_cur, to_cur))
    return float(amount) * rate if rate is not None else None

# ======================
# Routes
# ======================

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        departures = request.form.getlist("departures")
        dep_country = (request.form.get("departure_country") or "").strip()  # single
        arrival = request.form.get("arrival")
        arrival_countries = request.form.getlist("arrival_countries")

        date_from_s = request.form.get("date_from")
        date_to_s = request.form.get("date_to")
        currency = (request.form.get("currency") or "EUR").upper()
        max_price_s = (request.form.get("max_price") or "").strip()
        one_way = (request.form.get("one_way_only") == "on")
        min_stay_s = request.form.get("min_stay")
        max_stay_s = request.form.get("max_stay")
        out_weekday_s = request.form.get("out_weekday") or ""
        try:
            page = max(1, int(request.form.get("page") or "1"))
        except ValueError:
            page = 1
        PER_PAGE = 48

        errors = []
        valid_codes = {c for c, _ in AIRPORTS}

        # departures: country wins (single)
        if dep_country:
            dep_airports = sorted({a["code"] for a in AIRPORTS_RAW if a["country_code"] == dep_country})
            if not dep_airports:
                errors.append("No departure airports found for selected departure country.")
        else:
            if not departures:
                errors.append("Select at least one Departure airport or a Departure country.")
                dep_airports = []
            else:
                bad = [d for d in departures if d not in valid_codes]
                if bad: errors.append(f"Invalid departure airport(s): {', '.join(bad)}")
                dep_airports = departures

        # arrivals: multi-country allowed
        if arrival_countries:
            dest_airports = sorted({a["code"] for a in AIRPORTS_RAW if a["country_code"] in arrival_countries})
            if not dest_airports:
                errors.append("No arrival airports found for selected destination countries.")
        else:
            if not arrival:
                errors.append("Select Arrival airport or at least one Destination country.")
                dest_airports = []
            elif arrival not in valid_codes:
                errors.append("Invalid Arrival airport.")
                dest_airports = []
            else:
                dest_airports = [arrival]

        # dates
        try:
            d_from = datetime.strptime(date_from_s, "%Y-%m-%d").date()
            d_to = datetime.strptime(date_to_s, "%Y-%m-%d").date()
            if d_from > d_to:
                errors.append("Departure 'From' date must be before 'To' date.")
        except Exception:
            errors.append("Invalid dates.")
            d_from = d_to = None

        # stay
        min_stay = max_stay = None
        if not one_way:
            try:
                min_stay = int(min_stay_s); max_stay = int(max_stay_s)
                if min_stay <= 0 or max_stay <= 0: errors.append("Stay days must be positive.")
                if min_stay > max_stay: errors.append("Stay 'From' must be ≤ 'To'.")
            except Exception:
                errors.append("Provide valid stay days for round trips.")

        # max price
        max_price = None
        if max_price_s:
            try:
                max_price = float(max_price_s.replace(",", "."))
                if max_price <= 0: errors.append("Max price must be positive.")
            except Exception:
                errors.append("Max price must be a number (e.g. 299.99).")

        if errors:
            return render_template("index.html",
                                   airports=AIRPORTS, countries=COUNTRIES,
                                   errors=errors, form=request.form,
                                   today=date.today().isoformat())

        market = MARKET_BY_CURRENCY.get(currency, "en-ie")
        ua = {"User-Agent": "Mozilla/5.0"}
        wanted_weekday = int(out_weekday_s) if out_weekday_s.isdigit() else None

        results = []
        try:
            if one_way:
                for dep in dep_airports:
                    for arr in dest_airports:
                        url = (
                            "https://www.ryanair.com/api/farfnd/3/oneWayFares/"
                            f"{dep}/{arr}/cheapestPerDay"
                            f"?outboundDateFrom={d_from:%Y-%m-%d}"
                            f"&outboundDateTo={d_to:%Y-%m-%d}"
                            f"&market={market}&adultPaxCount=1"
                        )
                        r = requests.get(url, headers=ua, timeout=30); r.raise_for_status()
                        data = r.json()
                        
                        # Parse the correct response structure
                        outbound_data = data.get("outbound", {})
                        fares = outbound_data.get("fares", [])
                        
                        for fare in fares:
                            # Skip unavailable flights
                            if fare.get("unavailable", True) or fare.get("price") is None:
                                continue
                                
                            # Extract date from 'day' field
                            day_str = fare.get("day")
                            if not day_str:
                                continue
                                
                            # Extract price information
                            price_obj = fare.get("price", {})
                            raw_price = price_obj.get("value")
                            raw_curr = price_obj.get("currencyCode", currency).upper()
                            
                            # Parse the date
                            try:
                                d = datetime.strptime(day_str, "%Y-%m-%d").date()
                            except:
                                continue
                                
                            if d and raw_price is not None:
                                if wanted_weekday is not None and d.weekday() != wanted_weekday:
                                    continue
                                conv = convert_price(raw_price, raw_curr, currency)
                                effective_price = conv if conv is not None else (raw_price if raw_curr == currency else None)
                                if effective_price is None:
                                    continue
                                if max_price is not None and effective_price > max_price:
                                    continue
                                out_iso = d.isoformat()
                                results.append({
                                    "route": f"{dep} → {arr}",
                                    "out_date": out_iso,
                                    "price": f"{effective_price:.2f}",
                                    "currency": currency,
                                    "link": build_ryanair_link(True, dep, arr, out_iso)
                                })
            else:
                for dep in dep_airports:
                    for arr in dest_airports:
                        url = (
                            "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
                            f"?departureAirportIataCode={dep}"
                            f"&arrivalAirportIataCode={arr}"
                            f"&outboundDepartureDateFrom={d_from:%Y-%m-%d}"
                            f"&outboundDepartureDateTo={d_to:%Y-%m-%d}"
                            f"&inboundDepartureDateFrom={d_from:%Y-%m-%d}"
                            f"&inboundDepartureDateTo={(d_to + timedelta(days=max_stay)):%Y-%m-%d}"
                            f"&durationFrom={min_stay}&durationTo={max_stay}"
                            f"&market={market}&adultPaxCount=1"
                        )
                        r = requests.get(url, headers=ua, timeout=45); r.raise_for_status()
                        data = r.json()
                        fares = data.get("fares") or data.get("trips") or []
                        for combo in fares:
                            out = combo.get("outbound"); inn = combo.get("inbound")
                            out_d = parse_iso_date(out.get("departureDate") if out else None) or parse_iso_date(out.get("date") if out else None)
                            in_d  = parse_iso_date(inn.get("departureDate") if inn else None) or parse_iso_date(inn.get("date") if inn else None)
                            if not out_d or not in_d: continue
                            if wanted_weekday is not None and out_d.weekday() != wanted_weekday: continue
                            price = None; raw_curr = currency
                            if combo.get("price"):
                                p = combo["price"]; price = p.get("value") or p.get("amount"); raw_curr = (p.get("currencyCode") or p.get("currency") or currency).upper()
                            elif combo.get("summary") and combo["summary"].get("price"):
                                p = combo["summary"]["price"]; price = p.get("value") or p.get("amount"); raw_curr = (p.get("currencyCode") or p.get("currency") or currency).upper()
                            if price is None: continue
                            conv = convert_price(price, raw_curr, currency)
                            effective_price = conv if conv is not None else (price if raw_curr == currency else None)
                            if effective_price is None: continue
                            if max_price is not None and effective_price > max_price: continue
                            results.append({
                                "route": f"{dep} → {arr}",
                                "out_date": out_d.isoformat(), "in_date": in_d.isoformat(),
                                "price": f"{effective_price:.2f}", "currency": currency,
                                "link": build_ryanair_link(False, dep, arr, out_d.isoformat(), in_d.isoformat())
                            })
        except Exception as e:
            # error surfaced as a user-friendly message, Sentry will capture stack if enabled
            return render_template("index.html",
                                   airports=AIRPORTS, countries=COUNTRIES,
                                   errors=[f"Search failed: {e}"],
                                   form=request.form, today=date.today().isoformat())

        # sort + paginate
        try: results.sort(key=lambda x: float(x["price"]))
        except: pass
        total = len(results)
        PER_PAGE = 48
        total_pages = max(1, math.ceil(total / PER_PAGE))
        page = min(max(1, page), total_pages)
        start = (page - 1) * PER_PAGE; end = start + PER_PAGE
        page_results = results[start:end]

        form_payload = {
            "departures": departures,
            "departure_country": dep_country,
            "arrival": arrival, "arrival_countries": arrival_countries,
            "date_from": date_from_s, "date_to": date_to_s,
            "currency": currency, "max_price": max_price_s,
            "one_way_only": "on" if one_way else "",
            "min_stay": min_stay_s, "max_stay": max_stay_s,
            "out_weekday": out_weekday_s
        }

        return render_template("results.html",
                               one_way=one_way, results=page_results,
                               page=page, total_pages=total_pages, total=total, per_page=PER_PAGE,
                               form_payload=form_payload)

    return render_template("index.html",
                           airports=AIRPORTS, countries=COUNTRIES,
                           today=date.today().isoformat())

if __name__ == "__main__":
    # On production, run behind a WSGI server (gunicorn/uwsgi). This is dev server.
    app.run(use_reloader=True, debug=True)
