from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import mercadopago, os, re

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)
AR = timezone(timedelta(hours=-3))

PRECIO_GENERAL = 55_000
PRECIO_SOCIO   = 40_000

def fmt_date(s):
    if not s:
        return "-"
    try:
        # Python < 3.11: fromisoformat no acepta microsegundos con 'Z' ni '+00:00' completo
        # Normalizar: reemplazar Z por +00:00 y truncar microsegundos si hace falta
        s2 = s.replace("Z", "+00:00")
        # Intentar parseo directo
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            # Quitar microsegundos si hay 7 dígitos: 2026-10-15T12:34:56.1234567+00:00
            import re as _re
            s2 = _re.sub(r'(\.\d{6})\d+', r'\1', s2)
            dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(AR).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return s or "-"

def calcular_entradas(monto):
    """Descompone monto en entradas Generales ($55.000) y Socios ($40.000).
    Prueba combinaciones de 1-4 entradas con tolerancia ±5%."""
    monto = round(monto)
    tol = 0.05
    for total in range(1, 5):
        for g in range(total + 1):
            s = total - g
            esperado = g * PRECIO_GENERAL + s * PRECIO_SOCIO
            if esperado == 0:
                continue
            if abs(monto - esperado) / esperado <= tol:
                partes = []
                if g: partes.append(f"{g} Gral")
                if s: partes.append(f"{s} Socio")
                return " + ".join(partes)
    return "-"

def comision(p):
    return sum(f.get("amount", 0) for f in (p.get("fee_details") or []))

def is_received(p, uid):
    col = str(p.get("collector_id", ""))
    root_payer = str(p.get("payer_id", ""))
    return col == uid and root_payer != uid

def is_sent(p, uid):
    return str(p.get("payer_id", "")) == uid

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/pagos")
def pagos():
    token      = request.args.get("token", "").strip()
    tipo       = request.args.get("tipo", "received")
    desde_str  = request.args.get("desde", "")
    hasta_str  = request.args.get("hasta", "")
    estado_f   = request.args.get("estado", "").strip()
    fecha_ex   = request.args.get("fecha_exacta", "").strip()
    monto_min  = request.args.get("monto_min", "").strip()
    monto_max  = request.args.get("monto_max", "").strip()
    limite_str = request.args.get("limite", "200").strip()

    if not token:
        return jsonify({"error": "Token requerido"}), 400

    try:
        sdk = mercadopago.SDK(token)
        user_resp = sdk.user().get()
        user_data = user_resp.get("response", {})
        uid = str(user_data.get("id", ""))
        if not uid:
            return jsonify({"error": "No se pudo obtener el user_id del token"}), 400

        try:    limite = int(limite_str)
        except: limite = 200

        try:    mmin = float(monto_min.replace(",", ".")) if monto_min else None
        except: mmin = None
        try:    mmax = float(monto_max.replace(",", ".")) if monto_max else None
        except: mmax = None

        desde_dt = datetime.strptime(desde_str, "%Y-%m-%d") if desde_str else (datetime.now(AR) - timedelta(days=30))
        hasta_dt = datetime.strptime(hasta_str, "%Y-%m-%d") if hasta_str else datetime.now(AR)
        # Usar NOW() como end_date si hasta es hoy o futuro, para no perder pagos recientes
        hoy = datetime.now(AR).date()
        begin_date = desde_dt.strftime("%Y-%m-%dT00:00:00.000-03:00")
        if hasta_dt.date() >= hoy:
            end_date = datetime.now(AR).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
        else:
            end_date = hasta_dt.strftime("%Y-%m-%dT23:59:59.999-03:00")

        filtros = {
            "sort": "date_created", "criteria": "desc",
            "limit": limite, "offset": 0,
            "begin_date": begin_date, "end_date": end_date,
        }
        if estado_f:
            filtros["status"] = estado_f

        r = sdk.payment().search(filters=filtros)
        all_p = r.get("response", {}).get("results", [])

        # Filtrar por tipo
        if tipo == "received":
            pagos_f = [p for p in all_p if is_received(p, uid)]
        elif tipo == "sent":
            pagos_f = [p for p in all_p if is_sent(p, uid)]
        else:
            pagos_f = all_p

        # Pre-calcular fecha_ex como objeto date (comparación robusta)
        dt_ex_date = None
        if fecha_ex:
            try:
                dt_ex_date = datetime.strptime(fecha_ex, "%Y-%m-%d").date()
            except Exception:
                dt_ex_date = None

        # Filtros locales
        rows = []
        for p in pagos_f:
            amt = float(p.get("transaction_amount") or 0)

            if mmin is not None and amt < mmin:
                continue
            if mmax is not None and amt > mmax:
                continue

            # Parsear fecha del pago en zona Argentina para comparar
            raw_fecha = p.get("date_created", "")
            fecha_fmt = fmt_date(raw_fecha)

            if dt_ex_date is not None:
                try:
                    s2 = raw_fecha.replace("Z", "+00:00")
                    import re as _re2
                    s2 = _re2.sub(r'(\.\d{6})\d+', r'\1', s2)
                    dt_pago = datetime.fromisoformat(s2)
                    if dt_pago.tzinfo is None:
                        dt_pago = dt_pago.replace(tzinfo=timezone.utc)
                    pago_date = dt_pago.astimezone(AR).date()
                    if pago_date != dt_ex_date:
                        continue
                except Exception:
                    # fallback: comparar string
                    if dt_ex_date.strftime("%d/%m/%Y") not in fecha_fmt:
                        continue

            com = comision(p)
            monto_neto = amt - com
            st = p.get("status", "-")

            entradas = "-"
            if tipo == "received" and st == "approved":
                entradas = calcular_entradas(amt)

            rows.append({
                "id":         p.get("id", "-"),
                "fecha":      fecha_fmt,
                "estado":     st,
                "monto":      amt,
                "entradas":   entradas,
                "monto_neto": monto_neto,
                "acumulado":  None,
            })

        # Calcular acumulado (solo approved, en orden cronológico)
        rows.sort(key=lambda r: r["fecha"])
        acum = 0.0
        tot_monto = 0.0
        tot_neto  = 0.0
        pendientes = 0
        entradas_gral = 0
        entradas_socio = 0

        for r in rows:
            if r["estado"] == "approved":
                acum += r["monto"]
                r["acumulado"] = acum
                tot_monto += r["monto"]
                tot_neto  += r["monto_neto"]

                # Contar entradas
                det = r["entradas"]
                if det != "-":
                    for num, tp in re.findall(r'(\d+)\s+(Gral|Socio)', det):
                        if tp == "Gral":   entradas_gral  += int(num)
                        else:              entradas_socio += int(num)

            if r["estado"] in ("pending", "in_process"):
                pendientes += 1

        # Texto entradas vendidas
        total_e = entradas_gral + entradas_socio
        partes = []
        if entradas_gral:  partes.append(f"{entradas_gral} Gral")
        if entradas_socio: partes.append(f"{entradas_socio} Socio")
        entradas_txt = f"{total_e} ({' + '.join(partes)})" if partes else "0"

        return jsonify({
            "rows": rows,
            "stats": {
                "registros":   len(rows),
                "tot_monto":   tot_monto,
                "tot_neto":    tot_neto,
                "pendientes":  pendientes,
                "entradas_txt": entradas_txt,
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug")
def debug():
    """Muestra fechas crudas de los últimos 5 pagos — para diagnosticar formato."""
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"error": "Token requerido"}), 400
    try:
        sdk = mercadopago.SDK(token)
        r = sdk.payment().search(filters={"sort":"date_created","criteria":"desc","limit":5,"offset":0})
        results = r.get("response", {}).get("results", [])
        muestra = []
        for p in results:
            raw = p.get("date_created", "")
            muestra.append({
                "id": p.get("id"),
                "date_created_raw": raw,
                "fmt_date_result": fmt_date(raw),
            })
        return jsonify(muestra)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
