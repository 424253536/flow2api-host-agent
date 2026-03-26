# 使用轻量级的 Python 3.9 官方环境
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 先把依赖清单复制进去（利用缓存机制）
COPY requirements.txt .

# 安装项目所需的 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 将项目里的所有代码复制进容器
COPY . .

# 自动生成一个默认的配置文件，防止程序找不到文件暴毙
RUN cp agent.example.toml agent.toml

# 暴露作者指定的 38110 端口
EXPOSE 38110

# 🪄 终极魔法：写一个双拼启动脚本！
# 让 daemon 在后台运行（加个 & 符号），让 login 在前台运行
RUN echo '#!/bin/bash\n\
echo "启动后台守护进程 (daemon)..."\n\
python scripts/agent.py --config agent.toml daemon &\n\
echo "启动前台登录网页 (login)..."\n\
python scripts/agent.py --config agent.toml login\n\
' > start.sh && chmod +x start.sh

# 告诉 Docker，启动容器时执行我们的双拼脚本
CMD ["./start.sh"]
