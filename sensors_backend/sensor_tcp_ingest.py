#!/usr/bin/env python3
import faulthandler
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
import time
from typing import Dict, List, Optional, Tuple

_daily_alert_sent_on: Optional[date] = None
_fault_log_fp = None
_crash_logging_installed = False


def set_process_name(name: str) -> None:
    """Set the name shown by `ps -A` (Linux task comm, max 15 chars)."""
    comm = name[:15]
    try:
        with open("/proc/self/comm", "w", encoding="ascii", errors="replace") as f:
            f.write(comm)
    except OSError:
        pass
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        pr_set_name = 15
        buf = ctypes.create_string_buffer(comm.encode("ascii", "replace"))
        libc.prctl(pr_set_name, buf, 0, 0, 0)
    except Exception:
        pass


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


def _fault_log_path(log_file: str) -> str:
    base, ext = os.path.splitext(log_file)
    if ext.lower() == ".log":
        return base + ".fault.log"
    return log_file + ".fault.log"


def install_crash_logging(
    logger: logging.Logger,
    log_file: str,
    fault_log_file: Optional[str] = None,
) -> None:
    """Log uncaught exceptions (main + threads) and enable faulthandler dumps."""
    global _fault_log_fp, _crash_logging_installed
    if _crash_logging_installed:
        return
    _crash_logging_installed = True

    fault_path = fault_log_file or _fault_log_path(log_file)
    try:
        _fault_log_fp = open(fault_path, "ab", buffering=0)
        faulthandler.enable(file=_fault_log_fp, all_threads=True)
        logger.info("faulthandler enabled, dumps to %s", fault_path)
    except OSError as e:
        _fault_log_fp = None
        logger.warning("faulthandler not enabled (%s): %s", fault_path, e)

    def _flush_logs() -> None:
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        if _fault_log_fp is not None:
            try:
                _fault_log_fp.flush()
            except Exception:
                pass

    def _excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.error("uncaught exception", exc_info=(exc_type, exc, tb))
        _flush_logs()

    def _thread_excepthook(args) -> None:
        if args.exc_type is None or issubclass(args.exc_type, (SystemExit, KeyboardInterrupt)):
            return
        thread_name = args.thread.name if args.thread is not None else "unknown"
        logger.error(
            "uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _flush_logs()

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


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


def _run_daily_alert_curl(body: str, url: str, logger: logging.Logger) -> None:
    try:
        proc = subprocess.run(
            ["curl", "-d", body, url],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            logger.warning(
                "daily alert curl failed rc=%s stderr=%s stdout=%s",
                proc.returncode,
                (proc.stderr or "").strip(),
                (proc.stdout or "").strip(),
            )
        else:
            logger.info("daily alert curl ok")
    except subprocess.TimeoutExpired:
        logger.warning("daily alert curl timed out after 5s")
    except Exception:
        logger.exception("daily alert curl error")


def send_sensor_daily_alert(conn, logger: logging.Logger) -> None:
    yesterday = date.today() - timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sensors;")
        sensors_got = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(DISTINCT sensor_id)
            FROM temps_raw
            WHERE datetime::date = %s::date;
            """,
            (yesterday,),
        )
        sensors_reported = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT s.id, COUNT(tr.sensor_id) AS cnt, MIN(tr.battery) AS min_battery
            FROM sensors s
            LEFT JOIN temps_raw tr
              ON tr.sensor_id = s.id AND tr.datetime::date = %s::date
            GROUP BY s.id
            ORDER BY s.id ASC;
            """,
            (yesterday,),
        )
        per_sensor_rows = list(cur.fetchall())

    if sensors_got == sensors_reported:
        summary = f"All {sensors_reported} sensor(s) are alive."
    else:
        summary = f"From {sensors_got} sensor(s) only {sensors_reported} sensor(s) reported yesterday."

    detail_lines = []
    for sid, cnt, min_battery in per_sensor_rows:
        bat_s = "n/a" if min_battery is None else f"{float(min_battery):.2f}"
        detail_lines.append(f"Sensor {int(sid)} - {int(cnt)} reports, min battery {bat_s} V.")
    body = summary + ("\n" + "\n".join(detail_lines) if detail_lines else "")

    url = os.environ.get("ALERT_CURL_URL", "http://192.168.123.55:10000/alerts")
    logger.info("daily alert POST body: %s (curl in background, 5s max)", body)
    threading.Thread(
        target=_run_daily_alert_curl,
        args=(body, url, logger),
        daemon=True,
        name="daily-alert-curl",
    ).start()


def maybe_send_daily_sensor_alert(conn, logger: logging.Logger) -> None:
    global _daily_alert_sent_on
    if _daily_alert_sent_on == date.today():
        return
    now = datetime.now()
    cutoff = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now < cutoff:
        return
    try:
        send_sensor_daily_alert(conn, logger)
        _daily_alert_sent_on = date.today()
    except Exception:
        logger.exception("daily sensor alert failed")


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


def coerce_orp_sensor_payload(
    obj: dict,
) -> Optional[Tuple[int, Optional[float], Optional[float]]]:
    if obj.get("type") != "orp_sensor":
        return None

    dev_id = obj.get("dev_id")
    if dev_id is None:
        return None

    try:
        sensor_id = int(dev_id)
    except (TypeError, ValueError):
        return None

    orp = obj.get("orp")
    temp = obj.get("temp")

    try:
        orp_mv = None if orp is None else float(orp)
    except (TypeError, ValueError):
        orp_mv = None

    try:
        temp_f = None if temp is None else float(temp)
    except (TypeError, ValueError):
        temp_f = None

    return sensor_id, orp_mv, temp_f


def read_orp_settings(conn) -> Tuple[Optional[float], Optional[float]]:
    with conn.cursor() as cur:
        cur.execute("SELECT orp_offset, ph FROM orp_settings LIMIT 1;")
        row = cur.fetchone()
    if not row:
        return None, None
    orp_offset, ph = row
    return (
        float(orp_offset) if orp_offset is not None else None,
        float(ph) if ph is not None else None,
    )


# linux/tcp.h: 8-byte header + 9 u32s, then last_data_sent, last_ack_sent, last_data_recv.
_TCP_INFO_LAST_DATA_RECV_OFF = 52
_TCP_INFO_AGE_MS_MAX = 7 * 24 * 3600 * 1000


def socket_recv_datetime(sock: socket.socket) -> Tuple[datetime, Optional[int]]:
    """
    Wall time when the kernel last received TCP payload on this socket.

    Uses TCP_INFO.tcpi_last_data_recv (ms ago). That clock is updated when the
    segment arrives, not when userspace reads, so data that sat in the accept
    queue still gets its original receive time.
    Returns (datetime, age_ms). age_ms is None if TCP_INFO was unavailable.
    """
    now = datetime.now().astimezone()
    try:
        tcp_info = getattr(socket, "TCP_INFO", 11)
        raw = sock.getsockopt(socket.IPPROTO_TCP, tcp_info, 256)
        if len(raw) >= _TCP_INFO_LAST_DATA_RECV_OFF + 4:
            age_ms = struct.unpack_from("<I", raw, _TCP_INFO_LAST_DATA_RECV_OFF)[0]
            if 0 < age_ms < _TCP_INFO_AGE_MS_MAX:
                return now - timedelta(milliseconds=age_ms), age_ms
            return now, age_ms
    except OSError:
        pass
    return now, None


def insert_orp_raw(
    conn,
    logger: logging.Logger,
    sensor_id: int,
    orp_mv: Optional[float],
    orp_offset: Optional[float],
    temp: Optional[float],
    ph: Optional[float],
    received_at: datetime,
) -> None:
    sql = (
        "INSERT INTO orp_raw (datetime, sensor_id, orp_mv, orp_offset, temp, ph) "
        "VALUES (%s, %s, %s, %s, %s, %s);"
    )
    params = (received_at, sensor_id, orp_mv, orp_offset, temp, ph)
    logger.info("SQL: %s params=%s", sql, params)
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


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


def aggregate_today_for_sensor(
    conn, logger: logging.Logger, sensor_id: int, received_at: datetime
) -> None:
    """
    Builds 96 values (15-min intervals) for received_at's date and upserts into temps_aggr.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM date_trunc('day', %s::timestamptz))::bigint;",
            (received_at,),
        )
        midnight_epoch = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM datetime)::bigint AS ts, temp, humidity
            FROM temps_raw
            WHERE sensor_id = %s AND datetime::date = (%s::timestamptz)::date
            ORDER BY datetime ASC;
            """,
            (sensor_id, received_at),
        )
        rows: List[Tuple[int, Optional[float], Optional[int]]] = list(cur.fetchall())
        cur.execute(
            """
            SELECT battery
            FROM temps_raw
            WHERE sensor_id = %s
              AND datetime::date = (%s::timestamptz)::date
              AND battery IS NOT NULL
            ORDER BY datetime DESC
            LIMIT 1;
            """,
            (sensor_id, received_at),
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
    VALUES ((%s::timestamptz)::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    params = (
        received_at,
        sensor_id,
        t_min,
        t_max,
        t_avg,
        h_min,
        h_max,
        h_avg,
        latest_battery,
        temps_str,
        hum_str,
    )
    logger.info("SQL: %s params=%s", " ".join(sql.split()), params)

    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()
    logger.info(
        "aggregation upserted for sensor_id=%s day=%s",
        sensor_id,
        received_at.date().isoformat(),
    )


def insert_temp_raw(
    conn,
    logger: logging.Logger,
    sensor_id: int,
    temp: Optional[float],
    humidity: Optional[int],
    battery: Optional[float],
    received_at: datetime,
) -> None:
    sql = (
        "INSERT INTO temps_raw (datetime, sensor_id, temp, humidity, battery) "
        "VALUES (%s, %s, %s, %s, %s);"
    )
    params = (received_at, sensor_id, temp, humidity, battery)
    logger.info("SQL: %s params=%s", sql, params)
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def get_client_timeout() -> float:
    raw = os.environ.get("CLIENT_TIMEOUT", "10")
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 10.0
    return timeout if timeout > 0 else 10.0


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
    timeout_s = get_client_timeout()
    try:
        client_sock.settimeout(timeout_s)
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
                    received_at, age_ms = socket_recv_datetime(client_sock)
                    if age_ms is not None and age_ms >= 2000:
                        logger.info(
                            "using kernel rx time %s (%ss before now) for %s",
                            received_at.isoformat(timespec="seconds"),
                            round(age_ms / 1000, 1),
                            client_addr,
                        )

                    msg_type = obj.get("type")
                    if msg_type == "temp_sensor":
                        payload = coerce_temp_sensor_payload(obj)
                        if payload is None:
                            break

                        sensor_id, temp, humidity, battery = payload
                        insert_temp_raw(
                            conn_pg, logger, sensor_id, temp, humidity, battery, received_at
                        )

                        now_ts = int(time.time())
                        last_ts = last_agg_by_sensor.get(sensor_id, 0)
                        delayed = age_ms is not None and age_ms >= 30_000
                        if delayed or now_ts - last_ts >= 15 * 60:
                            last_agg_by_sensor[sensor_id] = now_ts
                            try:
                                aggregate_today_for_sensor(
                                    conn_pg, logger, sensor_id, received_at
                                )
                            except Exception:
                                logger.exception("aggregation failed for sensor_id=%s", sensor_id)
                    elif msg_type == "orp_sensor":
                        payload = coerce_orp_sensor_payload(obj)
                        if payload is None:
                            break

                        sensor_id, orp_mv, temp = payload
                        orp_offset, ph = read_orp_settings(conn_pg)
                        insert_orp_raw(
                            conn_pg, logger, sensor_id, orp_mv, orp_offset, temp, ph, received_at
                        )
                    break
    except socket.timeout:
        logger.warning("connection %s timed out waiting for data after %ss", client_addr, timeout_s)
    except Exception:
        logger.exception("connection %s error", client_addr)
    finally:
        logger.info("client disconnected: %s", client_addr)
    maybe_send_daily_sensor_alert(conn_pg, logger)
    return got_json


def main() -> int:
    global _daily_alert_sent_on
    set_process_name("sensor_tcp_ingest")
    logger = setup_logging()
    install_crash_logging(
        logger,
        os.environ.get("LOG_FILE", "sensor_tcp_ingest.log"),
        os.environ.get("FAULT_LOG_FILE"),
    )
    conn_pg = connect_pg()
    conn_pg.autocommit = False
    last_agg_by_sensor: Dict[int, int] = {}

    if _daily_alert_sent_on is None:
        try:
            send_sensor_daily_alert(conn_pg, logger)
            _daily_alert_sent_on = date.today()
        except Exception:
            logger.exception("initial daily sensor alert failed")

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
            except Exception:
                logger.exception("handler error from %s", client_addr)
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
                    logger.exception("DB connection lost; reconnecting")
                    try:
                        conn_pg.close()
                    except Exception:
                        pass
                    conn_pg = connect_pg()
                    conn_pg.autocommit = False

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
