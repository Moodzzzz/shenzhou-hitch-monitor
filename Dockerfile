FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

COPY . /app

# 纯标准库实现，无需 pip install；如需 YAML 配置或 OSS 存储再自行安装
# RUN pip install --no-cache-dir pyyaml oss2

VOLUME ["/app/data"]
CMD ["python", "main.py", "watch"]
