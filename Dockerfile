# 使用轻量级的 Python 3.9 官方环境
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 先把依赖清单复制进去（利用缓存机制）
COPY requirements.txt .

# 安装项目所需的 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 将你项目里的所有代码复制进容器
COPY . .

# 暴露 8080 端口（假设项目的 Web 界面跑在这个端口）
EXPOSE 8080

# 启动项目的核心指令（通常 Python 项目入口是 main.py 或 app.py）
# ⚠️ 注意：如果这个项目的启动文件不叫 main.py，请把你仓库里那个 .py 文件的名字替换到这里
CMD ["python", "web/main.py"]
