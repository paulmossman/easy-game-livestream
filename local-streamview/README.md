

Run:
```
docker compose up -d
```

Stream to it:
```
rtmp://<Docker-Host>:1936/live/stream
```

View stream:
```
http://<Docker-Host>:8888/live/stream/index.m3u8
```
If no stream to it, then `404 page not found`.

Test liveness when there is no stream:
```
http://<Docker-Host>:8888/v3/paths/list
```