#!/usr/bin/env python3
import logging
import os
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

from sensor_tcp_ingest import connect_pg, load_dotenv


def setup_logging() -> logging.Logger:
    load_dotenv()

    log_file = os.environ.get("API_LOG_FILE", "sensor_api_server.log")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    logger = logging.getLogger("sensor_api_server")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_000_000,
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    return logger


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        n = date(year + 1, 1, 1)
    else:
        n = date(year, month + 1, 1)
    return (n - date(year, month, 1)).days


def _avg_from_temps_string(s: Optional[str], logger: logging.Logger) -> Optional[float]:
    if not s:
        return None
    vals: List[float] = []
    for p in s.split(","):
        p = p.strip()
        if not p or p.lower() == "null":
            continue
        try:
            vals.append(float(p))
        except ValueError:
            logger.warning("bad temp value in temps_aggr: %s", p)
    if not vals:
        return None
    return sum(vals) / len(vals)


def create_app() -> Flask:
    load_dotenv()
    logger = setup_logging()
    app = Flask(__name__, static_folder="html", static_url_path="/static")

    @app.before_request
    def _log_request():
        logger.info("request %s %s from %s", request.method, request.path, request.remote_addr)

    @app.get("/")
    def index():
        logger.info("endpoint hit: /")
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/<path:filename>")
    def html_files(filename: str):
        if not filename.endswith(".html"):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(app.static_folder, filename)

    @app.get("/api/devices")
    def devices():
        """
        Returns [{id: <sensor_id>, name: <name or null>}, ...]
        sensor_id list comes from temps_raw; name is optional from sensors table.
        """
        logger.info("endpoint hit: /api/devices")
        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT tr.sensor_id AS id, s.name AS name
                    FROM temps_raw tr
                    LEFT JOIN sensors s ON s.id = tr.sensor_id
                    ORDER BY tr.sensor_id ASC;
                    """
                )
                rows = cur.fetchall()
            resp: List[Dict[str, Any]] = [{"id": int(r[0]), "name": r[1]} for r in rows]
            return jsonify(resp)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.post("/api/temp_day")
    def temp_day():
        """
        Body JSON: { "day": "YYYY-MM-DD" }
        Returns:
          {
            "day": "YYYY-MM-DD",
            "series": [
              {
                "sensor_id": 123,
                "name": "...",
                "points": [
                  { "ts": "<ISO-8601 timestamptz>", "temp": 21.5, "humidity": 48 },
                  ...
                ]
              },
              ...
            ]
          }
        """
        logger.info("endpoint hit: /api/temp_day")
        data = request.get_json(silent=True) or {}
        day_raw = data.get("day")

        if not isinstance(day_raw, str):
            return jsonify({"error": "day must be a string like YYYY-MM-DD"}), 400

        try:
            day = datetime.strptime(day_raw, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "day must be in format YYYY-MM-DD"}), 400

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tr.sensor_id, s.name, tr.datetime, tr.temp, tr.humidity
                    FROM temps_raw tr
                    LEFT JOIN sensors s ON s.id = tr.sensor_id
                    WHERE tr.datetime::date = %s::date
                    ORDER BY tr.sensor_id ASC, tr.datetime ASC;
                    """,
                    (day,),
                )
                rows = list(cur.fetchall())

            by_sensor: Dict[int, Dict[str, Any]] = {}
            for sensor_id, name, dt, temp, humidity in rows:
                sid = int(sensor_id)
                if sid not in by_sensor:
                    by_sensor[sid] = {"sensor_id": sid, "name": name, "points": []}
                ts_str = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                by_sensor[sid]["points"].append(
                    {
                        "ts": ts_str,
                        "temp": float(temp) if temp is not None else None,
                        "humidity": int(humidity) if humidity is not None else None,
                    }
                )

            series = [by_sensor[k] for k in sorted(by_sensor.keys())]

            return jsonify(
                {
                    "day": day.isoformat(),
                    "series": series,
                }
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.post("/api/temp_month")
    def temp_month():
        """
        Body JSON (optional): { "year": 2026, "month": 3 }
        Defaults to current month/year when missing.
        Returns:
          {
            "year": 2026,
            "month": 3,
            "days_in_month": 31,
            "series": [
              { "sensor_id": 123, "name": "...", "temps": [..days..], "humidities": [..days..] },
              ...
            ]
          }
        """
        logger.info("endpoint hit: /api/temp_month")
        data = request.get_json(silent=True) or {}

        now = datetime.now()
        year = data.get("year", now.year)
        month = data.get("month", now.month)
        try:
            year = int(year)
            month = int(month)
        except Exception:
            return jsonify({"error": "year and month must be integers"}), 400
        if month < 1 or month > 12:
            return jsonify({"error": "month must be 1..12"}), 400

        dim = _days_in_month(year, month)
        first = date(year, month, 1)
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH sensor_ids AS (
                      SELECT DISTINCT tr.sensor_id AS sensor_id
                      FROM temps_raw tr
                      WHERE tr.datetime >= %s::date AND tr.datetime < %s::date
                    )
                    SELECT si.sensor_id, s.name, ta.day, ta.temps, ta.humidities
                    FROM sensor_ids si
                    LEFT JOIN sensors s ON s.id = si.sensor_id
                    LEFT JOIN temps_aggr ta
                      ON ta.sensor_id = si.sensor_id
                     AND ta.day >= %s::date AND ta.day < %s::date
                    ORDER BY si.sensor_id ASC, ta.day ASC NULLS LAST;
                    """,
                    (first, nxt, first, nxt),
                )
                rows = list(cur.fetchall())

            # Build per-sensor arrays of length dim
            series_map: Dict[int, Dict[str, Any]] = {}
            for sensor_id, name, day_val, temps_s, hum_s in rows:
                sensor_id = int(sensor_id)
                if sensor_id not in series_map:
                    series_map[sensor_id] = {
                        "sensor_id": sensor_id,
                        "name": name,
                        "temps": [None] * dim,
                        "humidities": [None] * dim,
                    }
                if day_val is None:
                    continue
                day_idx = (day_val - first).days
                if 0 <= day_idx < dim:
                    series_map[sensor_id]["temps"][day_idx] = _avg_from_temps_string(temps_s, logger)
                    series_map[sensor_id]["humidities"][day_idx] = _avg_from_temps_string(hum_s, logger)

            return jsonify(
                {
                    "year": year,
                    "month": month,
                    "days_in_month": dim,
                    "series": list(series_map.values()),
                }
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.post("/api/batteries")
    def batteries():
        """
        Body JSON (optional): { "year": 2026, "month": 4 }
        Defaults to current month/year when missing.
        Sensor list is DISTINCT sensor_id from temps_aggr (not limited to the month).
        Returns battery samples in the requested month only, oldest day first per sensor:
          {
            "year": 2026,
            "month": 4,
            "days_in_month": 30,
            "series": [
              {
                "sensor_id": 123,
                "name": "...",
                "points": [ { "day": "YYYY-MM-DD", "battery": 3.7 }, ... ]
              },
              ...
            ]
          }
        """
        logger.info("endpoint hit: /api/batteries")
        data = request.get_json(silent=True) or {}

        now = datetime.now()
        year = data.get("year", now.year)
        month = data.get("month", now.month)
        try:
            year = int(year)
            month = int(month)
        except Exception:
            return jsonify({"error": "year and month must be integers"}), 400
        if month < 1 or month > 12:
            return jsonify({"error": "month must be 1..12"}), 400

        dim = _days_in_month(year, month)
        first = date(year, month, 1)
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH sensor_ids AS (
                      SELECT DISTINCT ta.sensor_id AS sensor_id
                      FROM temps_aggr ta
                    )
                    SELECT si.sensor_id, s.name, ta.day, ta.battery
                    FROM sensor_ids si
                    LEFT JOIN sensors s ON s.id = si.sensor_id
                    LEFT JOIN temps_aggr ta
                      ON ta.sensor_id = si.sensor_id
                     AND ta.day >= %s::date AND ta.day < %s::date
                     AND ta.battery IS NOT NULL
                    ORDER BY si.sensor_id ASC, ta.day ASC NULLS LAST;
                    """,
                    (first, nxt),
                )
                rows = list(cur.fetchall())

            series_map: Dict[int, Dict[str, Any]] = {}
            for sensor_id, name, day_val, bat in rows:
                sid = int(sensor_id)
                if sid not in series_map:
                    series_map[sid] = {"sensor_id": sid, "name": name, "points": []}
                if day_val is None or bat is None:
                    continue
                series_map[sid]["points"].append(
                    {"day": day_val.isoformat(), "battery": float(bat)}
                )

            series = [series_map[k] for k in sorted(series_map.keys())]

            return jsonify(
                {
                    "year": year,
                    "month": month,
                    "days_in_month": dim,
                    "series": series,
                }
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.post("/api/temp_year")
    def temp_year():
        """
        Body JSON (optional): { "year": 2026 }
        Defaults to current year when missing.
        Returns:
          {
            "year": 2026,
            "series": [
              {
                "sensor_id": 123,
                "name": "...",
                "months": [
                  { "month": 1, "t_min": ..., "t_max": ..., "t_avg": ... },
                  ...
                ]
              },
              ...
            ]
          }
        """
        logger.info("endpoint hit: /api/temp_year")
        data = request.get_json(silent=True) or {}
        year = data.get("year", datetime.now().year)
        try:
            year = int(year)
        except Exception:
            return jsonify({"error": "year must be an integer"}), 400

        start = date(year, 1, 1)
        end = date(year + 1, 1, 1)

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH sensor_ids AS (
                      SELECT DISTINCT tr.sensor_id AS sensor_id
                      FROM temps_raw tr
                      WHERE tr.datetime >= %s::date AND tr.datetime < %s::date
                    ),
                    month_stats AS (
                      SELECT
                        ta.sensor_id,
                        EXTRACT(MONTH FROM ta.day)::int AS month,
                        MIN(ta.t_min) AS t_min,
                        MAX(ta.t_max) AS t_max,
                        AVG(ta.t_avg) AS t_avg
                      FROM temps_aggr ta
                      WHERE ta.day >= %s::date AND ta.day < %s::date
                      GROUP BY ta.sensor_id, EXTRACT(MONTH FROM ta.day)
                    )
                    SELECT si.sensor_id, s.name, ms.month, ms.t_min, ms.t_max, ms.t_avg
                    FROM sensor_ids si
                    LEFT JOIN sensors s ON s.id = si.sensor_id
                    LEFT JOIN month_stats ms ON ms.sensor_id = si.sensor_id
                    ORDER BY si.sensor_id ASC, ms.month ASC NULLS LAST;
                    """,
                    (start, end, start, end),
                )
                rows = list(cur.fetchall())

            series_map: Dict[int, Dict[str, Any]] = {}
            for sensor_id, name, month, t_min, t_max, t_avg in rows:
                sensor_id = int(sensor_id)
                if sensor_id not in series_map:
                    series_map[sensor_id] = {"sensor_id": sensor_id, "name": name, "months": []}
                if month is None:
                    continue
                series_map[sensor_id]["months"].append(
                    {"month": int(month), "t_min": t_min, "t_max": t_max, "t_avg": t_avg}
                )

            return jsonify({"year": year, "series": list(series_map.values())})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return app


def main() -> int:
    app = create_app()
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "20000"))
    logging.getLogger("sensor_api_server").info("Starting Flask API on %s:%s", host, port)
    threads = int(os.environ.get("API_THREADS", "4"))
    serve(app, host=host, port=port, threads=threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


