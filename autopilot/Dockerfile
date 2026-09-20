FROM python:3.13-slim
WORKDIR /app
COPY app.py .
COPY douban_helper.py .
COPY subtitle_helper.py .
EXPOSE 8787
CMD ["python", "app.py"]
