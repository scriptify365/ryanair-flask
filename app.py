from flask import Flask, request, render_template
import requests
from datetime import datetime, timedelta, date
import math

app = Flask(__name__)

# ====== helpers i dane (bez zmian merytorycznych z poprzedniej wersji) ======
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
            "code": code,
            "label": label,
            "country_code": country_code,
            "country_name": country_name
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

# ====== ROUTES ======
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        departures = request.form.getlist("departures")
        arrival = request.form.get("arrival")
        arrival_countries = request.form.getlist("arrival_countries")
        date_from_s = request.form.get("date_from")
        date_to_s = request.form.get("date_to")
        currency = request.form.get("currency") or "EUR"
        one_way = (request.form.get("one_way_only") == "on")
        min_stay_s = request.form.get("min_stay")
        max_stay_s = request.form.get("max_stay")
        out_weekday_s = request.form.get("out_weekday") or ""
        try:
            page = int(request.form.get("page") or "1")
            if page < 1: page = 1
        except ValueError:
            page = 1
        PER_PAGE = 50

        errors = []
        valid_codes = {c for c, _ in AIRPORTS}
        if not departures:
            errors.append("Select at least one Departure airport.")
        else:
            for d in departures:
                if d not in valid_codes:
                    errors.append(f"Invalid departure airport: {d}")
                    break

        if arrival_countries:
            dest_airports = sorted({a["code"] for a in AIRPORTS_RAW if a["country_code"] in arrival_countries})
        else:
            if not arrival:
                errors.append("Select Arrival airport or at least one Destination country.")
                dest_airports = []
            elif arrival not in valid_codes:
                errors.append("Invalid Arrival airport.")
                dest_airports = []
            else:
                dest_airports = [arrival]

        try:
            d_from = datetime.strptime(date_from_s, "%Y-%m-%d").date()
            d_to = datetime.strptime(date_to_s, "%Y-%m-%d").date()
            if d_from > d_to:
                errors.append("Departure 'from' date must be before 'to' date.")
        except Exception:
            errors.append("Invalid dates (use the pickers).")
            d_from = d_to = None

        min_stay = max_stay = None
        if not one_way:
            try:
                min_stay = int(min_stay_s); max_stay = int(max_stay_s)
                if min_stay <= 0 or max_stay <= 0:
                    errors.append("Stay days must be positive.")
                if min_stay > max_stay:
                    errors.append("Stay 'from' must be <= 'to'.")
            except Exception:
                errors.append("Provide valid stay days for round trips.")

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
                for dep in departures:
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
                        fares = data.get("fares") or data.get("outbound") or data
                        if isinstance(fares, list):
                            for f in fares:
                                outbound = f.get("outbound") or f
                                ds = outbound.get("departureDate") or outbound.get("date")
                                price_info = outbound.get("price") or {}
                                price = price_info.get("value") or price_info.get("amount")
                                curr = price_info.get("currencyCode") or price_info.get("currency") or currency
                                d = parse_iso_date(ds)
                                if d and price is not None:
                                    if wanted_weekday is not None and d.weekday() != wanted_weekday:
                                        continue
                                    out_iso = d.isoformat()
                                    results.append({
                                        "route": f"{dep} → {arr}",
                                        "out_date": out_iso,
                                        "price": f"{float(price):.2f}",
                                        "currency": curr,
                                        "link": build_ryanair_link(True, dep, arr, out_iso)
                                    })
            else:
                for dep in departures:
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
                            price = None; curr = currency
                            if combo.get("price"):
                                p = combo["price"]; price = p.get("value") or p.get("amount"); curr = p.get("currencyCode") or p.get("currency") or curr
                            elif combo.get("summary") and combo["summary"].get("price"):
                                p = combo["summary"]["price"]; price = p.get("value") or p.get("amount"); curr = p.get("currencyCode") or p.get("currency") or curr
                            if price is None: continue
                            results.append({
                                "route": f"{dep} → {arr}",
                                "out_date": out_d.isoformat(), "in_date": in_d.isoformat(),
                                "price": f"{float(price):.2f}", "currency": curr,
                                "link": build_ryanair_link(False, dep, arr, out_d.isoformat(), in_d.isoformat())
                            })
        except Exception as e:
            return render_template("index.html",
                                   airports=AIRPORTS, countries=COUNTRIES,
                                   errors=[f"Search failed: {e}"],
                                   form=request.form, today=date.today().isoformat())

        # sort + paginacja (jak poprzednio)
        try: results.sort(key=lambda x: float(x["price"]))
        except: pass

        PER_PAGE = 50
        total = len(results)
        total_pages = max(1, math.ceil(total / PER_PAGE))
        page = min(max(1, page), total_pages)
        start = (page - 1) * PER_PAGE; end = start + PER_PAGE
        page_results = results[start:end]

        form_payload = {
            "departures": departures, "arrival": arrival, "arrival_countries": arrival_countries,
            "date_from": date_from_s, "date_to": date_to_s, "currency": currency,
            "one_way_only": "on" if one_way else "", "min_stay": min_stay_s, "max_stay": max_stay_s,
            "out_weekday": out_weekday_s
        }

        return render_template("results.html",
                               one_way=one_way, results=page_results,
                               page=page, total_pages=total_pages, total=total, per_page=PER_PAGE,
                               form_payload=form_payload)

    # GET → formularz, domyślnie date_from = dziś
    return render_template("index.html",
                           airports=AIRPORTS, countries=COUNTRIES,
                           today=date.today().isoformat())
