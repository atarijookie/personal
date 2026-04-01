# Simple sensors ingest and web view server

## Setup

1. install postgres server
2. run `setup_sensors_db.sh`
3. if you need to log into psql, you can: `sudo -u postgres psql`
4. run server as: `./sensor_server_start.sh`
5. to post data to sensor_tcp_ingest.py, do the following:
```
printf '%s\n' '{"type":"temp_sensor","dev_id":123,"packet_id":1111,"temp":20.1,"humidity":60,"battery":4.4}' | nc 127.0.0.1 22222
```
