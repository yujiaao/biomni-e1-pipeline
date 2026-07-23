# Biomni-E1 复现工程 — 容器化执行环境
# 版本全锁定，保证可复现（文章 Phase 5.3 规则 5-A / 5-B）
FROM continuumio/miniconda3:latest

WORKDIR /app

# 系统工具（按需开启，对应文章附录 A 技术栈与生物信息 CLI）
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     samtools bedtools wget ca-certificates && rm -rf /var/lib/apt/lists/*

# conda 环境
COPY docker/environment.yml /app/docker/environment.yml
RUN conda env create -f /app/docker/environment.yml

# Python 依赖
COPY docker/requirements.txt /app/docker/requirements.txt
RUN /opt/conda/envs/biomni_e1/bin/pip install --no-cache-dir -r /app/docker/requirements.txt

# 工程代码
COPY . /app

ENV PATH=/opt/conda/envs/biomni_e1/bin:$PATH
ENV OPENAI_API_KEY=

CMD ["python", "src/fetch_papers.py", "--demo"]
