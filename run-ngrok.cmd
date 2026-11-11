@echo off
REM 只在这一层清掉代理，ngrok 免费版才肯启动；外面的 serve.py 仍走系统代理
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=
"C:\Users\Administrator\ngrok\ngrok.exe" http 8765 --domain=oblong-evaporate-lunchbox.ngrok-free.dev --log=stdout
