#!/usr/bin/env python3
import json
import logging
import os
import socket
import sys
from contextlib import closing
from datetime import date
from logging.handlers import RotatingFileHandler
import time
from typing import Dict, List, Optional, Tuple


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        return


def setup_logging() -> logging.Logger:
    load_dotenv()

    log_file = os.environ.get("LOG_FILE", "sensor_tcp_ingest.log")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    logger = logging.getLogger("sensor_tcp_ingest")
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


def get_db_config() -> Dict[str, str]:
    load_dotenv()
    cfg = {
        "host": os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST") or os.environ.get("DB_HOST") or "localhost",
        "port": os.environ.get("PGPORT") or os.environ.get("POSTGRES_PORT") or os.environ.get("DB_PORT") or "5432",
        "user": os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER") or os.environ.get("DB_USER") or "",
        "password": os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or os.environ.get("DB_PASSWORD") or "",
        "dbname": os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB") or os.environ.get("DB_NAME") or "",
    }
    missing = [k for k in ("user", "password", "dbname") if not cfg[k]]
    if missing:
        raise RuntimeError(
            "Missing DB settings in .env. Need at least user/password/dbname via "
            "PGUSER/PGPASSWORD/PGDATABASE (or POSTGRES_*/DB_*). Missing: " + ", ".join(missing)
        )
    return cfg


def connect_pg():
    try:
        import psycopg2  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("psycopg2 is required. Install: pip install psycopg2-binary") from e

    cfg = get_db_config()
    return psycopg2.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["dbname"],
    )


def parse_json_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def coerce_temp_sensor_payload(
    obj: dict,
) -> Optional[Tuple[int, Optional[float], Optional[int], Optional[float]]]:
    if obj.get("type") != "temp_sensor":
        return None

    dev_id = obj.get("dev_id")
    if dev_id is None:
        return None

    try:
        sensor_id = int(dev_id)
    except (TypeError, ValueError):
        return None

    temp = obj.get("temp")
    humidity = obj.get("humidity")
    battery = obj.get("battery")

    try:
        temp_f = None if temp is None else float(temp)
    except (TypeError, ValueError):
        temp_f = None

    try:
        hum_i = None if humidity is None else int(humidity)
    except (TypeError, ValueError):
        hum_i = None
    try:
        battery_f = None if battery is None else float(battery)
    except (TypeError, ValueError):
        battery_f = None

    return sensor_id, temp_f, hum_i, battery_f


def _closest_value(
    rows: List[Tuple[int, Optional[float], Optional[int]]], target_ts: int, start_idx: int
) -> Tuple[int, Optional[float], Optional[int], int]:
    """
    rows: list of (ts_epoch_seconds, temp, humidity) sorted by ts.
    Returns (ts, temp, humidity, new_idx) for the closest row to target_ts,
    using a forward-moving pointer (start_idx).
    """
    n = len(rows)
    if n == 0:
        raise ValueError("rows must not be empty")

    i = min(max(start_idx, 0), n - 1)
    while i + 1 < n and abs(rows[i + 1][0] - target_ts) <= abs(rows[i][0] - target_ts):
        i += 1
    return rows[i][0], rows[i][1], rows[i][2], i


def aggregate_today_for_sensor(conn, logger: logging.Logger, sensor_id: int) -> None:
    """
    Builds 96 values (15-min intervals) for today's date and upserts into temps_aggr.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT EXTRACT(EPOCH FROM date_trunc('day', now()))::bigint;")
        midnight_epoch = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM datetime)::bigint AS ts, temp, humidity
            FROM temps_raw
            WHERE sensor_id = %s AND datetime::date = CURRENT_DATE
            ORDER BY datetime ASC;
            """,
            (sensor_id,),
        )
        rows: List[Tuple[int, Optional[float], Optional[int]]] = list(cur.fetchall())
        cur.execute(
            """
            SELECT battery
            FROM temps_raw
            WHERE sensor_id = %s
              AND datetime::date = CURRENT_DATE
              AND battery IS NOT NULL
            ORDER BY datetime DESC
            LIMIT 1;
            """,
            (sensor_id,),
        )
        battery_row = cur.fetchone()
        latest_battery: Optional[float] = battery_row[0] if battery_row else None

    temps_vals: List[Optional[float]] = []
    hum_vals: List[Optional[int]] = []

    if rows:
        idx = 0
        for slot in range(96):
            target_ts = midnight_epoch + slot * 15 * 60
            ts, t, h, idx = _closest_value(rows, target_ts, idx)
            if abs(ts - target_ts) > 15 * 60:
                temps_vals.append(None)
                hum_vals.append(None)
            else:
                temps_vals.append(t)
                hum_vals.append(h)
    else:
        temps_vals = [None] * 96
        hum_vals = [None] * 96

    temps_str = ", ".join("null" if v is None else str(v) for v in temps_vals)
    hum_str = ", ".join("null" if v is None else str(v) for v in hum_vals)

    temps_nonnull = [v for v in temps_vals if v is not None]
    hum_nonnull = [float(v) for v in hum_vals if v is not None]
    t_min = min(temps_nonnull) if temps_nonnull else None
    t_max = max(temps_nonnull) if temps_nonnull else None
    t_avg = (sum(temps_nonnull) / len(temps_nonnull)) if temps_nonnull else None
    h_min = min(hum_nonnull) if hum_nonnull else None
    h_max = max(hum_nonnull) if hum_nonnull else None
    h_avg = (sum(hum_nonnull) / len(hum_nonnull)) if hum_nonnull else None

    sql = """
    INSERT INTO temps_aggr (day, sensor_id, t_min, t_max, t_avg, h_min, h_max, h_avg, battery, temps, humidities)
    VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (day, sensor_id) DO UPDATE
      SET t_min = EXCLUDED.t_min,
          t_max = EXCLUDED.t_max,
          t_avg = EXCLUDED.t_avg,
          h_min = EXCLUDED.h_min,
          h_max = EXCLUDED.h_max,
          h_avg = EXCLUDED.h_avg,
          battery = EXCLUDED.battery,
          temps = EXCLUDED.temps,
          humidities = EXCLUDED.humidities;
    """
    params = (sensor_id, t_min, t_max, t_avg, h_min, h_max, h_avg, latest_battery, temps_str, hum_str)
    logger.info("SQL: %s params=%s", " ".join(sql.split()), params)

    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()
    logger.info("aggregation upserted for sensor_id=%s day=%s", sensor_id, date.today().isoformat())


def insert_temp_raw(
    conn,
    logger: logging.Logger,
    sensor_id: int,
    temp: Optional[float],
    humidity: Optional[int],
    battery: Optional[float],
) -> None:
    sql = "INSERT INTO temps_raw (sensor_id, temp, humidity, battery) VALUES (%s, %s, %s, %s);"
    params = (sensor_id, temp, humidity, battery)
    logger.info("SQL: %s params=%s", sql, params)
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def handle_connection(
    conn_pg,
    logger: logging.Logger,
    last_agg_by_sensor: Dict[int, int],
    client_sock: socket.socket,
    client_addr,
) -> bool:
    """
    Returns True if at least one valid JSON line was received (regardless of type).
    """
    got_json = False
    try:
        with client_sock:
            logger.info("client connected: %s", client_addr)
            f = client_sock.makefile("r", encoding="utf-8", newline="\n")
            with closing(f):
                for raw_line in f:
                    logger.info("recv raw from %s: %s", client_addr, raw_line.rstrip("\n"))
                    obj = parse_json_line(raw_line)
                    if obj is None:
                        continue
                    got_json = True

                    payload = coerce_temp_sensor_payload(obj)
                    if payload is None:
                        # JSON received but not a temp_sensor payload; ignore
                        break

                    sensor_id, temp, humidity, battery = payload
                    insert_temp_raw(conn_pg, logger, sensor_id, temp, humidity, battery)

                    now_ts = int(time.time())
                    last_ts = last_agg_by_sensor.get(sensor_id, 0)
                    if now_ts - last_ts >= 15 * 60:
                        last_agg_by_sensor[sensor_id] = now_ts
                        try:
                            aggregate_today_for_sensor(conn_pg, logger, sensor_id)
                        except Exception as e:
                            logger.warning("aggregation failed for sensor_id=%s: %s", sensor_id, e)
                    break
    except Exception as e:
        logger.warning("connection %s error: %s", client_addr, e)
    finally:
        logger.info("client disconnected: %s", client_addr)
    return got_json


def main() -> int:
    logger = setup_logging()
    conn_pg = connect_pg()
    conn_pg.autocommit = False
    last_agg_by_sensor: Dict[int, int] = {}

    listen_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("LISTEN_PORT", "22222"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((listen_host, listen_port))
        s.listen(50)
        logger.info("Listening on %s:%s", listen_host, listen_port)

        while True:
            client_sock, client_addr = s.accept()
            try:
                _ = handle_connection(conn_pg, logger, last_agg_by_sensor, client_sock, client_addr)
            except Exception as e:
                logger.warning("handler error from %s: %s", client_addr, e)
                try:
                    conn_pg.rollback()
                except Exception:
                    pass
            finally:
                # If DB connection went bad, reconnect for next client
                try:
                    with conn_pg.cursor() as cur:
                        cur.execute("SELECT 1;")
                    conn_pg.commit()
                except Exception:
                    logger.warning("DB connection lost; reconnecting")
                    try:
                        conn_pg.close()
                    except Exception:
                        pass
                    conn_pg = connect_pg()
                    conn_pg.autocommit = False

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
