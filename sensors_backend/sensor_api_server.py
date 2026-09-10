#!/usr/bin/env python3
import logging
import os
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

from sensor_tcp_ingest import connect_pg, install_crash_logging, load_dotenv, set_process_name


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


def _optional_float(value: Any, field: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")


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

    @app.get("/api/orp_devices")
    def orp_devices():
        """
        Returns [{id: <sensor_id>, name: <name or null>}, ...]
        sensor_id list comes from orp_raw; name is optional from sensors table.
        """
        logger.info("endpoint hit: /api/orp_devices")
        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT tr.sensor_id AS id, s.name AS name
                    FROM orp_raw tr
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

    @app.get("/api/orp_settings")
    def get_orp_settings():
        logger.info("endpoint hit: GET /api/orp_settings")
        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT orp_offset, ph FROM orp_settings LIMIT 1;")
                row = cur.fetchone()
            if not row:
                return jsonify({"orp_offset": None, "ph": None})
            orp_offset, ph = row
            return jsonify(
                {
                    "orp_offset": float(orp_offset) if orp_offset is not None else None,
                    "ph": float(ph) if ph is not None else None,
                }
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.post("/api/orp_settings")
    def save_orp_settings():
        logger.info("endpoint hit: POST /api/orp_settings")
        data = request.get_json(silent=True) or {}
        try:
            orp_offset = _optional_float(data.get("orp_offset"), "orp_offset")
            ph = _optional_float(data.get("ph"), "ph")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM orp_settings;")
                count = int(cur.fetchone()[0])
                if count:
                    cur.execute(
                        "UPDATE orp_settings SET orp_offset = %s, ph = %s;",
                        (orp_offset, ph),
                    )
                else:
                    cur.execute(
                        "INSERT INTO orp_settings (orp_offset, ph) VALUES (%s, %s);",
                        (orp_offset, ph),
                    )
            conn.commit()
            return jsonify({"orp_offset": orp_offset, "ph": ph})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    orp_conversion_table = {
        70: [(735, 0.5), (760, 1.0), (772, 1.5), (780, 2.0), (788, 2.5), (794, 3.0), (800, 3.5)],
        71: [(725, 0.5), (750, 1.0), (765, 1.5), (772, 2.0), (780, 2.5), (785, 3.0), (792, 3.5), (798, 4.0)],
        72: [(720, 0.5), (742, 1.0), (756, 1.5), (763, 2.0), (772, 2.5), (778, 3.0), (782, 3.5), (786, 4.0), (792, 4.5), (798, 5.0)],
        73: [(712, 0.5), (734, 1.0), (748, 1.5), (756, 2.0), (763, 2.5), (769, 3.0), (774, 3.5), (778, 4.0), (782, 4.5), (785, 5.0), (789, 5.5), (793, 6.0), (796, 6.5)],
        74: [(704, 0.5), (725, 1.0), (738, 1.5), (748, 2.0), (754, 2.5), (761, 3.0), (765, 3.5), (770, 4.0), (774, 4.5), (778, 5.0), (781, 5.5), (783, 6.0), (786, 6.5), (788, 7.0), (792, 7.5)],
        75: [(695, 0.5), (716, 1.0), (731, 1.5), (740, 2.0), (746, 2.5), (753, 3.0), (758, 3.5), (762, 4.0), (766, 4.5), (769, 5.0), (772, 5.5), (775, 6.0), (777, 6.5), (780, 7.0), (782, 7.5), (784, 8.0), (786, 8.5), (788, 9.0), (790, 9.5), (792, 10.0)],
        76: [(687, 0.5), (709, 1.0), (722, 1.5), (732, 2.0), (738, 2.5), (745, 3.0), (750, 3.5), (754, 4.0), (757, 4.5), (761, 5.0), (764, 5.5), (767, 6.0), (770, 6.5), (772, 7.0), (774, 7.5), (776, 8.0), (778, 8.5), (780, 9.0), (782, 9.5), (784, 10.0)],
        77: [(680, 0.5), (703, 1.0), (715, 1.5), (724, 2.0), (732, 2.5), (737, 3.0), (742, 3.5), (746, 4.0), (751, 4.5), (754, 5.0), (756, 5.5), (760, 6.0), (762, 6.5), (765, 7.0), (767, 7.5), (769, 8.0), (771, 8.5), (773, 9.0), (775, 9.5), (777, 10.0)],
        78: [(675, 0.5), (695, 1.0), (708, 1.5), (717, 2.0), (725, 2.5), (731, 3.0), (735, 3.5), (739, 4.0), (743, 4.5), (746, 5.0), (750, 5.5), (753, 6.0), (756, 6.5), (758, 7.0), (760, 7.5), (762, 8.0), (764, 8.5), (766, 9.0), (767, 9.5), (769, 10.0)],
        79: [(668, 0.5), (689, 1.0), (702, 1.5), (712, 2.0), (718, 2.5), (724, 3.0), (729, 3.5), (734, 4.0), (736, 4.5), (741, 5.0), (744, 5.5), (746, 6.0), (749, 6.5), (752, 7.0), (754, 7.5), (756, 8.0), (758, 8.5), (760, 9.0), (762, 9.5), (763, 10.0)],
        80: [(662, 0.5), (684, 1.0), (697, 1.5), (705, 2.0), (714, 2.5), (719, 3.0), (724, 3.5), (728, 4.0), (733, 4.5), (736, 5.0), (738, 5.5), (742, 6.0), (744, 6.5), (746, 7.0), (749, 7.5), (751, 8.0), (753, 8.5), (755, 9.0), (756, 9.5), (758, 10.0)],
    }

    def orp_to_ppm(orp_measured, orp_offset, temp_C, ph):
        # use defaults for the offset, temperature and pH, if not provided
        orp_offset = 0 if orp_offset is None else orp_offset
        temp_C = 25 if temp_C is None else temp_C
        ph = 7 if ph is None else ph

        # only orp_measured is minimaly required for calculation, fail if it's not provided
        if orp_measured is None:
            logger.warning(f"orp_to_ppm - invalid input - orp_measured = {orp_measured}")
            return None

        # calc orp with the expected offset and compensate for temperature (table is for 25C)
        k = 1.5             # k is the temperature coefficient in mV/C, because ORP drops when water gets warmer
        orp_25 = orp_measured + orp_offset + k * (temp_C - 25)

        # convert pH from float to integer, so it can be used as stable dict key
        ph_key = int(ph * 10 + 0.5)
        if ph_key not in orp_conversion_table:
            logger.warning(f"orp_to_ppm - pH value {ph:.1f} ({ph_key}) not in the conversion table!")
            return None

        # Linear interpolation between nearest ORP entries
        points = orp_conversion_table[ph_key]

        # input orp too low?
        if orp_25 < points[0][0]:
            logger.warning(f"orp_to_ppm - input orp {orp_25} too low, returning 0")
            return 0

        # input orp too high?
        if orp_25 > points[-1][0]:
            logger.warning(f"orp_to_ppm - input orp {orp_25} too high, returning 10")
            return 10

        for i in range(len(points) - 1):
            orp1, ppm1 = points[i]
            orp2, ppm2 = points[i + 1]
            if orp1 <= orp_25 <= orp2:
                # Interpolate PPM
                t = (orp_25 - orp1) / (orp2 - orp1)
                return round(ppm1 + t * (ppm2 - ppm1), 2)

        return None

    @app.post("/api/orp_day")
    def orp_day():
        """
        Body JSON: { "day": "YYYY-MM-DD", "sensor_id": 7 }
        Returns one series for that sensor:
          {
            "day": "YYYY-MM-DD",
            "series": [
              {
                "sensor_id": 7,
                "name": "...",
                "points": [
                  { "ts": "<ISO-8601 timestamptz>", "orp": 750.0, "orp_offset": 10.0, "temp": 30.0, "ph": 7.2 },
                  ...
                ]
              }
            ]
          }
        """
        logger.info("endpoint hit: /api/orp_day")
        data = request.get_json(silent=True) or {}
        day_raw = data.get("day")
        sid_raw = data.get("sensor_id")

        if not isinstance(day_raw, str):
            return jsonify({"error": "day must be a string like YYYY-MM-DD"}), 400

        try:
            day = datetime.strptime(day_raw, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "day must be in format YYYY-MM-DD"}), 400

        try:
            sensor_id = int(sid_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "sensor_id must be an integer"}), 400

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT orp.sensor_id, s.name, orp.datetime, orp.orp_mv, orp.orp_offset, orp.temp, orp.ph
                    FROM orp_raw orp
                    LEFT JOIN sensors s ON s.id = orp.sensor_id
                    WHERE orp.datetime::date = %s::date
                      AND orp.sensor_id = %s
                    ORDER BY orp.datetime ASC;
                    """,
                    (day, sensor_id),
                )
                rows = list(cur.fetchall())

            by_sensor: Dict[int, Dict[str, Any]] = {}
            for sensor_id, name, dt, orp_mv, orp_offset, temp, ph in rows:
                ppm = orp_to_ppm(orp_mv, orp_offset, temp, ph)

                sid = int(sensor_id)
                if sid not in by_sensor:
                    by_sensor[sid] = {"sensor_id": sid, "name": name, "points": []}
                ts_str = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                by_sensor[sid]["points"].append(
                    {
                        "ts": ts_str,
                        "orp": float(orp_mv) if orp_mv is not None else None,
                        "orp_offset": float(orp_offset) if orp_offset is not None else None,
                        "temp": float(temp) if temp is not None else None,
                        "ph": float(ph) if ph is not None else None,
                        "ppm": float(ppm) if ppm is not None else None,
                    }
                )

            series = [by_sensor[k] for k in sorted(by_sensor.keys())]
            return jsonify({"day": day.isoformat(), "series": series})
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
              {
                "sensor_id": 123,
                "name": "...",
                "temps": [..daily avg from slot string..],
                "temps_min": [..t_min per day..],
                "temps_max": [..t_max per day..],
                "humidities": [..days..],
              },
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
                    SELECT si.sensor_id, s.name, ta.day, ta.temps, ta.humidities, ta.t_min, ta.t_max
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
            for sensor_id, name, day_val, temps_s, hum_s, t_min, t_max in rows:
                sensor_id = int(sensor_id)
                if sensor_id not in series_map:
                    series_map[sensor_id] = {
                        "sensor_id": sensor_id,
                        "name": name,
                        "temps": [None] * dim,
                        "temps_min": [None] * dim,
                        "temps_max": [None] * dim,
                        "humidities": [None] * dim,
                    }
                if day_val is None:
                    continue
                day_idx = (day_val - first).days
                if 0 <= day_idx < dim:
                    series_map[sensor_id]["temps"][day_idx] = _avg_from_temps_string(temps_s, logger)
                    series_map[sensor_id]["humidities"][day_idx] = _avg_from_temps_string(hum_s, logger)
                    if t_min is not None:
                        series_map[sensor_id]["temps_min"][day_idx] = float(t_min)
                    if t_max is not None:
                        series_map[sensor_id]["temps_max"][day_idx] = float(t_max)

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
    set_process_name("sensor_api_server")
    logger = setup_logging()
    install_crash_logging(
        logger,
        os.environ.get("API_LOG_FILE", "sensor_api_server.log"),
        os.environ.get("API_FAULT_LOG_FILE"),
    )
    app = create_app()
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "20000"))
    logging.getLogger("sensor_api_server").info("Starting Flask API on %s:%s", host, port)
    threads = int(os.environ.get("API_THREADS", "4"))
    serve(app, host=host, port=port, threads=threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


