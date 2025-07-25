# 🎵 Dyvine

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type Hints](https://img.shields.io/badge/typing-mypy-green.svg)](http://mypy-lang.org/)
[![Tests](https://img.shields.io/badge/tests-pytest-orange.svg)](https://pytest.org)

[📖 文档](#概述) • [🚀 快速开始](#快速开始) • [🔧 API 参考](#api-参考) • [🐳 部署](#部署) • [🤝 贡献](#贡献)

---

**Dyvine** 是一个生产就绪的高性能 REST API，专为内容管理而设计。提供全面的内容下载、用户管理、直播流处理和云存储集成功能。

**🎯 核心特性** • **⚡ 异步处理** • **🔄 批量操作** • **☁️ 云集成** • **📊 实时监控** • **🔐 安全**

## 概述

Dyvine 提供生产级可靠性的内容下载和管理综合 API。支持各种内容类型，包括视频、图片、直播流和用户信息，内置云存储集成。

### 主要特性

- **📥 内容管理**: 下载视频、图片和直播流
- **👥 用户操作**: 获取用户资料和内容分析
- **⚡ 批量处理**: 高效的批量内容下载操作
- **🏗️ 架构设计**: 具有连接池的异步操作
- **☁️ 云存储**: 直接集成对象存储
- **🔧 开发者体验**:
  - 完整的类型提示
  - 详细的错误消息和日志记录
  - 自动生成的 OpenAPI/Swagger 文档
  - 生产就绪的配置管理

## 快速开始

### 系统要求

- Python 3.12+
- Git
- 2GB+ 可用磁盘空间
- 活跃的互联网连接
- 有效的身份验证 cookie
- 可选：对象存储凭据

### 安装

```bash
# 克隆仓库
git clone https://github.com/memenow/dyvine.git
cd dyvine

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -e .

# 安装开发依赖（可选）
pip install -e ".[dev]"
```

### 配置

1. **环境设置**:

   ```bash
   cp .env.example .env
   ```

2. **必要配置**:

   编辑 `.env` 文件：

   ```bash
   # 基本设置
   DOUYIN_COOKIE=你的cookie
   
   # 安全设置（生产环境必须更改）
   SECURITY_SECRET_KEY=你的生产密钥
   SECURITY_API_KEY=你的生产API密钥
   
   # 可选：对象存储集成
   R2_ACCOUNT_ID=你的账户ID
   R2_ACCESS_KEY_ID=你的访问密钥
   R2_SECRET_ACCESS_KEY=你的秘密访问密钥
   R2_BUCKET_NAME=你的存储桶名称
   ```

### 运行应用

```bash
# 启动开发服务器
uvicorn src.dyvine.main:app --reload

# 生产服务器
uvicorn src.dyvine.main:app --host 0.0.0.0 --port 8000
```

API 将在以下地址可用：

- **应用程序**: <http://localhost:8000>
- **交互式文档**: <http://localhost:8000/docs>
- **备用文档**: <http://localhost:8000/redoc>

## API 参考

### 基础 URL

```text
http://localhost:8000/api/v1
```

### 核心端点

#### 用户操作

```http
# 获取用户信息
GET /api/v1/users/{user_id}

# 下载用户内容
POST /api/v1/users/{user_id}/content:download
```

#### 帖子管理

```http
# 获取帖子详情
GET /api/v1/posts/{post_id}

# 列出用户帖子
GET /api/v1/posts/users/{user_id}/posts

# 下载用户帖子
POST /api/v1/posts/users/{user_id}/posts:download
```

#### 直播流

```http
# 下载活跃直播流
POST /api/v1/livestreams/users/{user_id}/stream:download

# 从 URL 下载
POST /api/v1/livestreams/stream:download

# 检查下载状态
GET /api/v1/livestreams/operations/{operation_id}
```

### 使用示例

**下载用户帖子**:

```bash
curl -X POST "http://localhost:8000/api/v1/posts/users/用户ID/posts:download" \
     -H "Content-Type: application/json"
```

**获取用户信息**:

```bash
curl "http://localhost:8000/api/v1/users/用户ID"
```

## 测试

项目包含全面的测试套件，完全支持异步：

```bash
# 运行所有测试
pytest

# 运行覆盖率测试
pytest --cov=src/dyvine

# 运行特定测试类别
pytest tests/unit/          # 单元测试
pytest tests/integration/   # 集成测试

# 详细输出运行
pytest -v
```

### 测试结构

```text
tests/
├── unit/                 # 单元测试
│   ├── core/            # 核心功能
│   ├── routers/         # API 端点
│   ├── schemas/         # 数据模型
│   └── services/        # 业务逻辑
└── integration/         # 集成测试
    ├── test_api.py      # 完整 API 工作流
    └── test_douyin.py   # 外部服务集成
```

## 部署

### Docker 部署

1. **构建镜像**:

   ```bash
   docker build -t dyvine:latest -f deploy/Dockerfile .
   ```

2. **运行容器**:

   ```bash
   docker run -d \
     --name dyvine \
     -p 8000:8000 \
     -v $(pwd)/data:/app/data \
     -v $(pwd)/logs:/app/logs \
     --env-file .env \
     dyvine:latest
   ```

### Kubernetes 部署

1. **先决条件**:
   - Kubernetes 集群
   - 配置的 kubectl
   - 容器注册表访问权限

2. **部署**:

   ```bash
   # 更新 deploy/k8s.yaml 中的镜像引用
   kubectl apply -f deploy/k8s.yaml
   
   # 验证部署
   kubectl get pods -l app=dyvine
   kubectl get services dyvine
   ```

### 生产考虑

- **安全**: 使用适当的密钥管理（HashiCorp Vault）
- **监控**: 设置 Prometheus 指标和日志聚合
- **高可用**: 配置多副本和自动扩展
- **备份**: 实施持久卷和日志存档策略

## 监控和日志

### 健康监控

```http
GET /health
```

响应包括：

- 应用程序状态和版本
- 系统正常运行时间和资源使用情况
- 请求统计
- 内存和 CPU 指标

### 日志功能

- 结构化 JSON 日志，便于机器读取
- 请求关联跟踪
- 自动日志轮转和归档
- 开发/生产格式化模式
- 性能指标收集

### 开发命令

```bash
# 代码格式化
black .
isort .

# 类型检查
mypy .

# 代码检查
ruff check .

# 运行所有检查
pytest && black . && isort . && mypy . && ruff check .
```

## 许可证

该项目采用 Apache License 2.0 许可 - 详情请参阅 [LICENSE](LICENSE) 文件。
