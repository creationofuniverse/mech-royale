FROM python:3.13-slim
WORKDIR /bot
COPY . .
RUN chmod -R a+rX /bot && python -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
