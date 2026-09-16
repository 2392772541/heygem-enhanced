# HeyGem-Enhanced Docker 容器编排指南

本项目 deploy/ 目录针对不同硬件架构与运行场景，提供了 4 套生产就绪的 Docker Compose 编排方案。所有配置均已经过生产级加固：
1. **去除了 
estart: always**（改为 
estart: \ no\），防止系统开机时无序自启并竞争显存；
2. **解除了宿主机 10095 端口绑定**，防止与本地已有的 ASR 服务产生端口冲突；
3. **支持动态数据盘挂载**：统一通过 ${DUIX_DATA_DIR:-d:/duix_avatar_data} 环境变量控制存储目录。

---

## 编排方案对比与选型

| 配置文件 | 适用硬件与环境 | 核心容器与镜像 | 特性说明 |
| :--- | :--- | :--- | :--- |
| **docker-compose.yml**<br>*(推荐默认)* | **常规 Windows 生产机**<br>(RTX 3060 / 3090 / 4070 / 4090 等) | - duix-avatar-tts<br>  (guiji2025/fish-speech-ziming)<br>- duix-avatar-asr<br>  (guiji2025/fun-asr)<br>- duix-avatar-gen-video<br>  (guiji2025/duix.avatar) | 包含完整官方全功能组件。scripts/start-heygem.ps1 启动脚本默认调用本文件。 |
| **docker-compose-5090.yml** | **RTX 50 系列新架构显卡**<br>(RTX 5090 / 5080 / Blackwell 架构) | - duix-avatar-tts<br>  (guiji2025/fish-speech-5090)<br>- duix-avatar-gen-video<br>  (guiji2025/duix.avatar-5090) | 针对 CUDA 12.8+ 与 Blackwell 核心优化镜像，解决新架构显卡算力兼容问题；去除冗余 ASR，释放显存。 |
| **docker-compose-lite.yml** | **显存受限或使用外部 TTS**<br>(8GB/12GB 显存，或搭配 IndexTTS2) | - duix-avatar-gen-video<br>  (guiji2025/duix.avatar) | **纯视频渲染极简版**。不启动容器内的 TTS 和 ASR，TTS 完全由本地 IndexTTS2（18180端口）或云端 API 提供，显存占用直降 4~6GB。 |
| **docker-compose-linux.yml** | **Linux 原生 GPU 服务器**<br>(Ubuntu 22.04 / Debian 等云端服务器) | - duix-avatar-tts<br>- duix-avatar-asr<br>- duix-avatar-gen-video | 采用 Linux 原生路径（如 ~/duix_avatar_data），便于直接在 Linux 服务器上以纯无头模式运行后端渲染引擎。 |

---

## 手工启动与调试命令

### 1. 使用默认配置启动
`ash
cd deploy
docker compose -f docker-compose.yml up -d
`

### 2. 使用 RTX 5090 专有配置启动
`ash
cd deploy
docker compose -f docker-compose-5090.yml up -d
`

### 3. 使用轻量级极简版启动
`ash
cd deploy
docker compose -f docker-compose-lite.yml up -d
`

### 4. 查看容器运行日志
`ash
# 查看视频渲染核心日志
docker logs -f duix-avatar-gen-video

# 查看内置 TTS 日志
docker logs -f duix-avatar-tts
`

### 5. 停止容器
`ash
docker compose -f docker-compose.yml down
`

---

## 环境变量挂载说明

在 .env 或系统环境变量中设置 DUIX_DATA_DIR 可自由迁移数据目录：
- Windows 默认：DUIX_DATA_DIR=d:/duix_avatar_data（若迁移至 E 盘设为 :/duix_avatar_data）
- 容器内部映射结构：
  - ${DUIX_DATA_DIR}/face2face 挂载至 /code/data（视频生成与工作缓存）
  - ${DUIX_DATA_DIR}/voice/data 挂载至 /code/data（参考声音与底模音频）