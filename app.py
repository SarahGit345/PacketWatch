# app.py
from flask import Flask, render_template, Response, stream_with_context
import json, time, psutil, math, socket

app = Flask(__name__)

# ---- Rolling state for rates & health ----
_last = None                  # last snapshot (with timestamp)
ewma_health = None            # smoothed health score
ALPHA = 0.2                   # EWMA smoothing
WINDOW_PPS = []               # for burst z-score
W = 30                        # 30 seconds window

def read_counters():
    io = psutil.net_io_counters()  # system-wide counters
    # Connections: tcp/udp split
    tcp_conns = 0
    udp_conns = 0
    established = 0

    # inet covers tcp & udp; on some OS, separate udp() can be faster
    for c in psutil.net_connections(kind="inet"):
        try:
            if c.type == socket.SOCK_STREAM:
                tcp_conns += 1
                if getattr(psutil, "CONN_ESTABLISHED", "ESTABLISHED"):
                    if c.status == psutil.CONN_ESTABLISHED:
                        established += 1
            elif c.type == socket.SOCK_DGRAM:
                udp_conns += 1
        except Exception:
            # Ignore ephemeral permission issues on some OS
            pass

    return {
        "bytes_sent": io.bytes_sent,
        "bytes_recv": io.bytes_recv,
        "packets_sent": io.packets_sent,
        "packets_recv": io.packets_recv,
        "errin": io.errin,
        "errout": io.errout,
        "dropin": io.dropin,
        "dropout": io.dropout,
        "tcp_conns": tcp_conns,
        "udp_conns": udp_conns,
        "established": established
    }

def compute_rates(now, prev, cur):
    dt = max(1e-6, now - prev["t"])
    r = {
        "bytes_sec_sent": max(0, (cur["bytes_sent"]   - prev["bytes_sent"])   / dt),
        "bytes_sec_recv": max(0, (cur["bytes_recv"]   - prev["bytes_recv"])   / dt),
        "pkts_sec_sent":  max(0, (cur["packets_sent"] - prev["packets_sent"]) / dt),
        "pkts_sec_recv":  max(0, (cur["packets_recv"] - prev["packets_recv"]) / dt),
        "errin_delta":    max(0,  cur["errin"]   - prev["errin"]),
        "errout_delta":   max(0,  cur["errout"]  - prev["errout"]),
        "dropin_delta":   max(0,  cur["dropin"]  - prev["dropin"]),
        "dropout_delta":  max(0,  cur["dropout"] - prev["dropout"]),
    }
    r["bytes_sec_total"] = r["bytes_sec_sent"] + r["bytes_sec_recv"]
    r["pkts_sec_total"]  = r["pkts_sec_sent"]  + r["pkts_sec_recv"]
    return r

def health_score(rates):
    """0..100 score: more throughput = better; errors/drops penalize."""
    global ewma_health
    # 25 MB/s ~ near-100 (tweak to your NIC/env)
    throughput = rates["bytes_sec_total"] / (25 * 1024 * 1024)
    base = 100 * (1 - math.exp(-throughput))
    penalty = 0
    penalty += 5 * (rates["errin_delta"] + rates["errout_delta"])
    penalty += 3 * (rates["dropin_delta"] + rates["dropout_delta"])
    raw = max(0, min(100, base - penalty))
    ewma_health = raw if ewma_health is None else (ALPHA * raw + (1 - ALPHA) * ewma_health)
    return round(ewma_health, 1)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stream")
def stream():
    @stream_with_context
    def event_stream():
        global _last, WINDOW_PPS
        # prime baseline
        if _last is None:
            snap = read_counters()
            _last = {"t": time.time(), **snap}
            time.sleep(1)

        while True:
            now = time.time()
            cur = {"t": now, **read_counters()}
            rates = compute_rates(now, _last, cur)

            # burst detection (z-score over last W seconds)
            WINDOW_PPS.append(rates["pkts_sec_total"])
            if len(WINDOW_PPS) > W: WINDOW_PPS.pop(0)
            mean = sum(WINDOW_PPS)/len(WINDOW_PPS)
            var = sum((x-mean)**2 for x in WINDOW_PPS)/len(WINDOW_PPS) if len(WINDOW_PPS) > 1 else 0
            std = var**0.5
            z = (rates["pkts_sec_total"]-mean)/std if std > 1e-6 else 0
            burst = z > 2.5

            # simple anomaly flags
            anomalies = []
            if burst:
                anomalies.append({"type":"burst","msg":f"Burst: {rates['pkts_sec_total']:.1f} pkts/s"})
            if rates["dropin_delta"] + rates["dropout_delta"] > 0:
                anomalies.append({"type":"drop","msg":"Packet drops detected"})
            if rates["errin_delta"] + rates["errout_delta"] > 0:
                anomalies.append({"type":"error","msg":"Interface errors detected"})

            score = health_score(rates)

            payload = {
                "ts": int(now),
                "throughput_bps": int(rates["bytes_sec_total"]),
                "throughput_mbps": round(rates["bytes_sec_total"] / 1_000_000, 2),
                "pkts_per_sec": round(rates["pkts_sec_total"], 2),
                "errors": {
                    "in": int(rates["errin_delta"]),
                    "out": int(rates["errout_delta"]),
                    "drop_in": int(rates["dropin_delta"]),
                    "drop_out": int(rates["dropout_delta"]),
                },
                "conns": {
                    "tcp": cur["tcp_conns"],
                    "udp": cur["udp_conns"],
                    "established": cur["established"]
                },
                "health": score,
                "anomalies": anomalies
            }

            yield f"data: {json.dumps(payload)}\n\n"
            _last = cur
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    # pip install flask psutil
    app.run(debug=True, threaded=True)
