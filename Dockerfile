# 使用官方轻量级 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量，防止 Python 缓冲标准输出
ENV PYTHONUNBUFFERED=1

# 复制依赖清单并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目所有代码到容器内
COPY . .

# 暴露 Flask 默认端口
EXPOSE 5000

# 启动命令
CMD ["python", "app.py"]