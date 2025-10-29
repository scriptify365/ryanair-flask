import os
import time
import math
import logging
from datetime import datetime, timedelta, date

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from flask import Flask, request, render_template

app = Flask(__name__, template_folder="templates", static_folder="static")
app.logger.setLevel(logging.INFO)

# ---- Brand via ENV ----
@app.context_processor
def inject_brand():
    return {"brand": os.getenv("APP_BRAND", "Flynair")}

# ---- Robust HTTP: retries + pool + sane timeouts ----
def make_session():
    s = requests.Session()
    retries = Retry(
        total=3, connect=3, read=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

HTTP = make_session()
REQ_TIMEOUT = (5, 12)     # (connect, read) – krócej niż gunicorn timeout
AIRPORTS_TIMEOUT = (3, 8) # szybciej failuje dla listy lotnisk

# ---------- Ryanair airports ----------
def fetch_airports_data():
    url = "https://www.ryanair.com/api/views/locate/5/airports/pl/active"
    try:
        t0 = time.perf_counter()
        r = HTTP.get(url, timeout=AIRPORTS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        app.logger.info("GET airports %s in %.2fs", r.status_code, time.perf_counter()-t0)
    except Exception as e:
        app.logger.warning("[airports] %s", e)
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

# Market influences currency returned by Ryanair
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

# Very simple FX for display/filter
FX = {
    ("EUR", "PLN"): 4.30, ("PLN", "EUR"): 1/4.30,
    ("EUR", "GBP"): 0.86, ("GBP", "EUR"): 1/0.86,
    ("PLN", "GBP"): (1/4.30)*0.86, ("GBP", "PLN"): (1/0.86)*4.30,
}
def convert_price(amount, from_cur, to_cur):
    if from_cur == to_cur: return float(amount)
    rate = FX.get((from_cur, to_cur))
    return float(amount) * rate if rate is not None else None

# ---------- Routes ----------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Inputs
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

        # Validation
        errors = []
        valid_codes = {c for c, _ in AIRPORTS}

        # Departure set: country wins (single)
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

        # Arrivals: either single airport or multi-countries
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

        # Dates
        try:
            d_from = datetime.strptime(date_from_s, "%Y-%m-%d").date()
            d_to = datetime.strptime(date_to_s, "%Y-%m-%d").date()
            if d_from > d_to:
                errors.append("Departure 'From' date must be before 'To' date.")
        except Exception:
            errors.append("Invalid dates.")
            d_from = d_to = None

        # Stay (round-trip only)
        min_stay = max_stay = None
        if not one_way:
            try:
                min_stay = int(min_stay_s); max_stay = int(max_stay_s)
                if min_stay <= 0 or max_stay <= 0: errors.append("Stay days must be positive.")
                if min_stay > max_stay: errors.append("Stay 'From' must be ≤ 'To'.")
            except Exception:
                errors.append("Provide valid stay days for round trips.")

        # Max price
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

        # Build search
        market = MARKET_BY_CURRENCY.get(currency, "en-ie")
        wanted_weekday = int(out_weekday_s) if out_weekday_s.isdigit() else None

        results = []
        try:
            if one_way:
                # One-way fares
                for dep in dep_airports:
                    for arr in dest_airports:
                        url = (
                            "https://www.ryanair.com/api/farfnd/3/oneWayFares/"
                            f"{dep}/{arr}/cheapestPerDay"
                            f"?outboundDateFrom={d_from:%Y-%m-%d}"
                            f"&outboundDateTo={d_to:%Y-%m-%d}"
                            f"&market={market}&adultPaxCount=1"
                        )
                        t0 = time.perf_counter()
                        r = HTTP.get(url, timeout=REQ_TIMEOUT); r.raise_for_status()
                        data = r.json()
                        app.logger.info("GET oneWay %s in %.2fs", r.status_code, time.perf_counter()-t0)
                        fares = data.get("fares") or data.get("outbound") or data
                        if isinstance(fares, list):
                            for f in fares:
                                outbound = f.get("outbound") or f
                                ds = outbound.get("departureDate") or outbound.get("date")
                                price_info = outbound.get("price") or {}
                                raw_price = price_info.get("value") or price_info.get("amount")
                                raw_curr = (price_info.get("currencyCode") or price_info.get("currency") or currency).upper()
                                ddt = parse_iso_date(ds)
                                if ddt and raw_price is not None:
                                    if wanted_weekday is not None and ddt.weekday() != wanted_weekday:
                                        continue
                                    conv = convert_price(raw_price, raw_curr, currency)
                                    effective_price = conv if conv is not None else (raw_price if raw_curr == currency else None)
                                    if effective_price is None:
                                        continue
                                    if max_price is not None and effective_price > max_price:
                                        continue
                                    out_iso = ddt.isoformat()
                                    results.append({
                                        "route": f"{dep} → {arr}",
                                        "out_date": out_iso,
                                        "price": f"{effective_price:.2f}",
                                        "currency": currency,
                                        "link": build_ryanair_link(True, dep, arr, out_iso)
                                    })
            else:
                # Round-trip fares
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
                        t0 = time.perf_counter()
                        r = HTTP.get(url, timeout=REQ_TIMEOUT); r.raise_for_status()
                        data = r.json()
                        app.logger.info("GET rt %s in %.2fs", r.status_code, time.perf_counter()-t0)
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
            return render_template("index.html",
                                   airports=AIRPORTS, countries=COUNTRIES,
                                   errors=[f"Search failed: {e}"],
                                   form=request.form, today=date.today().isoformat())

        # Sort + paginate
        try: results.sort(key=lambda x: float(x["price"]))
        except: pass
        total = len(results)
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

        return render_template("result.html",
                               one_way=one_way, results=page_results,
                               page=page, total_pages=total_pages, total=total, per_page=PER_PAGE,
                               form_payload=form_payload)

    # GET → form
    return render_template("index.html",
                           airports=AIRPORTS, countries=COUNTRIES,
                           today=date.today().isoformat())

if __name__ == "__main__":
    app.run(use_reloader=True, debug=True)
