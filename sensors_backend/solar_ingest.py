#!/usr/bin/env python3
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from sensor_tcp_ingest import connect_pg, install_crash_logging, load_dotenv, set_process_name


def setup_logging() -> logging.Logger:
    load_dotenv()

    log_file = os.environ.get("SOLAR_LOG_FILE", "solar_ingest.log")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    logger = logging.getLogger("solar_ingest")
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


def to_signed_16(val: int) -> int:
    """Converts 16-bit unsigned to signed integer."""
    if val > 32767:
        val -= 65536
    return val


def read_u16(modbus, addr: int) -> int:
    return modbus.read_holding_registers(register_addr=addr, quantity=1)[0]


def get_logger_config() -> Dict[str, Any]:
    load_dotenv()
    sn_raw = (os.environ.get("LOGGER_SN") or "").strip()
    if not sn_raw:
        raise RuntimeError(
            "Missing LOGGER_SN in .env (Solarman/Deye logger serial number)."
        )
    try:
        serial = int(sn_raw)
    except ValueError as e:
        raise RuntimeError("LOGGER_SN must be an integer serial number.") from e

    interval_raw = os.environ.get("SOLAR_POLL_INTERVAL_SEC", "600")
    try:
        interval_sec = int(interval_raw)
    except ValueError:
        interval_sec = 600
    if interval_sec <= 0:
        interval_sec = 600

    return {
        "ip": os.environ.get("LOGGER_IP", "192.168.123.9"),
        "serial": serial,
        "port": int(os.environ.get("LOGGER_PORT", "8899")),
        "slave_id": int(os.environ.get("LOGGER_SLAVE_ID", "1")),
        "socket_timeout": float(os.environ.get("LOGGER_SOCKET_TIMEOUT", "5")),
        "interval_sec": interval_sec,
    }


def get_telemetry(cfg: Dict[str, Any], logger: logging.Logger) -> Optional[Dict[str, float]]:
    try:
        from pysolarmanv5 import PySolarmanV5
    except Exception as e:  # pragma: no cover
        raise RuntimeError("pysolarmanv5 is required. Install: pip install pysolarmanv5") from e

    modbus = None
    try:
        modbus = PySolarmanV5(
            address=cfg["ip"],
            serial=cfg["serial"],
            port=cfg["port"],
            mb_slave_id=cfg["slave_id"],
            verbose=False,
            socket_timeout=cfg["socket_timeout"],
        )

        # Deye three-phase LV hybrid (SG04LP3-style) holding registers. These
        # are 16-bit watts, not 32-bit. 0x025E/0x026A are per-phase CT readings.
        pv1_w_raw = read_u16(modbus, 0x02A0)
        pv2_w_raw = read_u16(modbus, 0x02A1)
        pv1_v_raw = read_u16(modbus, 0x02A4)
        pv1_a_raw = read_u16(modbus, 0x02A5)
        bat_v_raw = read_u16(modbus, 0x024B)
        soc_raw = read_u16(modbus, 0x024C)
        bat_w_raw = read_u16(modbus, 0x024E)
        grid_w_raw = read_u16(modbus, 0x0271)
        load_w_raw = read_u16(modbus, 0x028D)

        data = {
            "pv_power_w": float(pv1_w_raw + pv2_w_raw),
            "grid_power_w": float(to_signed_16(grid_w_raw)),
            "battery_power_w": float(to_signed_16(bat_w_raw)),
            "house_load_power_w": float(load_w_raw),
            "battery_soc_pct": round(soc_raw * 0.01 if soc_raw > 100 else soc_raw, 1),
        }
        logger.info(
            "telemetry pv=%sW (pv1=%sW pv2=%sW, %sV @ %sA) grid=%sW bat=%sW (%sV, SOC %s%%) load=%sW",
            data["pv_power_w"],
            pv1_w_raw,
            pv2_w_raw,
            round(pv1_v_raw * 0.1, 1),
            round(pv1_a_raw * 0.1, 1),
            data["grid_power_w"],
            data["battery_power_w"],
            round(bat_v_raw * 0.01, 2),
            data["battery_soc_pct"],
            data["house_load_power_w"],
        )
        return data
    except Exception:
        logger.exception("solar telemetry read failed")
        return None
    finally:
        if modbus is not None:
            try:
                modbus.disconnect()
            except Exception:
                logger.exception("solar logger disconnect failed")


def insert_solar_raw(
    conn,
    logger: logging.Logger,
    data: Dict[str, float],
    received_at: datetime,
) -> None:
    sql = (
        "INSERT INTO solar_raw "
        "(datetime, pv_power_w, grid_power_w, battery_power_w, house_load_power_w, battery_soc_pct) "
        "VALUES (%s, %s, %s, %s, %s, %s);"
    )
    params = (
        received_at,
        data["pv_power_w"],
        data["grid_power_w"],
        data["battery_power_w"],
        data["house_load_power_w"],
        data["battery_soc_pct"],
    )
    logger.info("SQL: %s params=%s", sql, params)
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def ensure_db(conn, logger: logging.Logger):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        conn.commit()
        return conn
    except Exception:
        logger.exception("DB connection lost; reconnecting")
        try:
            conn.close()
        except Exception:
            pass
        conn = connect_pg()
        conn.autocommit = False
        return conn


def main() -> int:
    set_process_name("solar_ingest")
    logger = setup_logging()
    install_crash_logging(
        logger,
        os.environ.get("SOLAR_LOG_FILE", "solar_ingest.log"),
        os.environ.get("SOLAR_FAULT_LOG_FILE"),
    )
    cfg = get_logger_config()
    conn = connect_pg()
    conn.autocommit = False
    logger.info(
        "polling %s:%s slave=%s every %ss",
        cfg["ip"],
        cfg["port"],
        cfg["slave_id"],
        cfg["interval_sec"],
    )

    while True:
        started = time.monotonic()
        conn = ensure_db(conn, logger)
        data = get_telemetry(cfg, logger)
        if data is not None:
            try:
                insert_solar_raw(conn, logger, data, datetime.now().astimezone())
            except Exception:
                logger.exception("insert failed")
                try:
                    conn.rollback()
                except Exception:
                    pass
        elapsed = time.monotonic() - started
        sleep_for = max(0.0, cfg["interval_sec"] - elapsed)
        logger.info("sleeping %.0fs until next poll", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
