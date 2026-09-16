# HeyGem-Enhanced (数字人全链条增强调度与生产就绪工程)

基于官方 **Duix.Avatar (HeyGem)** 深度二次开发与生产级加固的数字人音视频生成中枢。针对原版系统在多任务并发、排队死锁、Docker 容器冲突、音视频交付以及语音合成扩展性上的多项痛点，进行了彻底的架构重构与自愈机制增强。

---

## 目录
- [1. 核心改进与问题排查审计报告](#1-核心改进与问题排查审计报告)
- [2. 项目架构与目录划分](#2-项目架构与目录划分)
- [3. 在新电脑上的快速迁移与部署指南](#3-在新电脑上的快速迁移与部署指南)
- [4. 日常使用与启动器说明](#4-日常使用与启动器说明)
- [5. 高级配置与环境变量覆盖](#5-高级配置与环境变量覆盖)
- [6. 常见故障排查与运维](#6-常见故障排查与运维)

---

## 1. 核心改进与问题排查审计报告

在原版 HeyGem / Duix.Avatar 的实际落地与批量生产中，存在若干阻碍无人值守运行的严重问题。本项目已对其进行了系统性排查与加固：

| 痛点问题 | 原版表现 | HeyGem-Enhanced 解决方案 |
| :--- | :--- | :--- |
| **并发抢单与 Busy 假死** | 客户端与调度器同时发包导致后端报 `busy`，原脚本因连续 busy 强行重启正在渲染的容器，造成恶性循环。 | 引入 `CLIENT_SUBMIT_GRACE_SECONDS = 8` 客户端避让窗口；收到 busy 时自动关联 Docker 日志中的活跃任务 UUID 并在后台平滑接管，**严禁盲目重启渲染容器**。 |
| **数据库状态丢包/游离** | 前端偶发网络抖动或崩溃导致任务永远处于 `pending` 或 `waiting` 状态，后端无法消化。 | **SQLite 触发器自愈注入**：向业务库自动挂载自愈 trigger 与状态超时探测，对无宿主无任务码的任务自动回滚重试。 |
| **TTS 模式单一与音质局限** | 仅依赖 Docker 容器内的 Fish-Speech，遇到显存或生僻词易崩溃且音质难以满足专业口播。 | **双模 TTS 调度器**：研发 IndexTTS2 独立适配层（端口 18180），支持 Bilibili IndexTTS2 / IndexTTS2.5 零样本高保真克隆，并支持一键热切回 Fish-Speech。 |
| **成片交付无序与存储挤爆** | 成片与几十 GB 临时切片 WAV 混杂在临时目录中，无自动导出与校验机制。 | **存储与交付解耦**：保留 D: 盘为 Docker 内部高性能渲染缓存，通过 `deliver_video_copy()` 自动将成功成片经过 ffprobe 与哈希校验后，统一归档至 `E:\数字人视频文件`（或自定义交付盘）。 |
| **容器自启与端口冲突** | Docker Compose 默认配置 `restart: always`，开机无序自启并占用 `10095` 端口，与本地 ASR 竞争。 | 将容器全部调整为 `restart: "no"`，受控由启动脚本按序拉起；移除 10095 宿主机绑定，避免端口争用。 |
| **硬编码与跨机器移植** | 原版大量硬编码 `win10` 用户名、绝对盘符与本地模型名称，换电脑即瘫痪。 | **全链路环境自适应**：盘符自动探测（D/E/C/桌面动态 fallback），提供 `model_sources.example.json` 模板，脱敏配置全进 `.gitignore`。 |

---

## 2. 项目架构与目录划分

```text
HeyGem-Enhanced/
├── Start-HeyGem.bat                  # [根目录便捷入口] 一键启动完整服务链
├── Stop-HeyGem.bat                   # [根目录便捷入口] 一键安全优雅停止
├── Install-Shortcuts.bat             # [根目录便捷入口] 一键在桌面创建分类启动器
├── heygem.config.example.json        # 环境变量与高级路径配置示例
├── tts-mode.txt                      # 当前 TTS 模式指示文件 (index-tts2 / fish-speech)
│
├── core/                             # 核心服务与调度引擎
│   ├── heygem_queue_runner.py        # 2100+行 核心队列调度器、自愈触发器、交付闭环
│   └── heygem-queue-guard.ps1        # 后台守护进程（单实例保活、500探测与自愈）
│
├── scripts/                          # 运维与自动化执行脚本
│   ├── start-heygem.ps1              # 完整启动编排（Docker -> Compose -> TTS -> Guard -> App）
│   ├── stop-heygem.ps1               # 优雅停止编排
│   ├── switch-heygem-tts.ps1         # TTS 模式平滑切换
│   ├── export-heygem-videos.ps1      # 批量扫描并交付已完成成片
│   ├── cleanup_heygem_cache.py       # 安全清理孤儿 WAV、过期目录与超大日志
│   ├── heygem_bulk_delete.py         # 批量删除历史成片（安全保护模特底模）
│   ├── repair_heygem_models.py       # 模特底模音频与视频关联修复工具
│   ├── restore_heygem_model_videos.py# 模特视频跨机器备份恢复工具
│   ├── model_sources.example.json    # 模特备份映射配置文件模板
│   ├── view_heygem_process_status.py # 终端实时运行看板与任务健康监控
│   ├── download-full-client.ps1      # 客户端安装包断点续传下载工具
│   └── generate-desktop-shortcuts.ps1# 桌面快捷方式生成脚本
│
├── launchers/                        # 桌面启动器对应的无黑框批处理
│   ├── Start-HeyGem.bat
│   ├── Stop-HeyGem.bat
│   ├── View-Status.bat
│   ├── Pause-Queue.bat
│   ├── Resume-Queue.bat
│   ├── Switch-TTS-Index.bat
│   ├── Switch-TTS-FishSpeech.bat
│   ├── Export-Videos.bat
│   ├── Cleanup-Cache.bat
│   └── Bulk-Delete-Works.bat
│
├── deploy/                           # Docker 部署配置文件 (优化版)
│   ├── docker-compose.yml            # 主容器配置 (restart: "no", 修复10095端口)
│   ├── docker-compose-lite.yml       # 纯视频生成极简配置
│   ├── docker-compose-5090.yml       # RTX 5090 / 新显卡专有配置
│   └── docker-compose-linux.yml      # Linux 部署兼容配置
│
├── adapter/                          # 外部扩展适配服务
│   ├── heygem_index_tts2_adapter.py  # 18180 端口 IndexTTS2 高清语音克隆适配服务
│   ├── config.json                   # IndexTTS2 模型推理参数配置
│   └── config.example.json
│
└── icons/                            # 高清 UI 状态图标 (10 枚)
    ├── start.ico, stop.ico, pause.ico, resume.ico, index.ico, fish.ico...
```

---

## 3. 在新电脑上的快速迁移与部署指南

在新电脑（推荐 Windows 10/11 64位，配备 NVIDIA RTX 显卡）上部署只需以下步骤：

### 第一步：基础运行环境准备
1. **安装 Git 与 Python**：推荐 Python 3.10 或 3.11（安装时勾选 `Add python.exe to PATH`）。
   - **调度器核心零依赖**：主队列调度器与所有维护脚本完全使用 Python 标准库，无需额外 pip 安装。
   - **双模 TTS 可选依赖**：若需使用 IndexTTS2 高保真语音克隆，可在克隆后执行：
     ```powershell
     pip install -r adapter\requirements.txt
     ```
     *(注：若未安装或未部署 IndexTTS2，系统启动时会自动智能降级为内置官方 Fish-Speech 模式，不影响数字人视频渲染。)*
2. **安装 Docker Desktop**：
   - 启用 WSL2 后端；
   - 确保安装并启用 NVIDIA Container Toolkit（NVIDIA 显卡驱动需支持 CUDA）。

### 第二步：克隆本仓库
```powershell
git clone https://github.com/2392772541/heygem-enhanced.git D:\HeyGem-Enhanced
cd D:\HeyGem-Enhanced
```

### 第三步：安装 HeyGem 官方桌面客户端
运行自带的断点续传下载脚本：
```powershell
powershell -ExecutionPolicy Bypass -File scripts\download-full-client.ps1
```
下载完成后双击安装 `Duix.Avatar-1.0.6-setup.exe`（默认安装在 `C:\Program Files\Duix.Avatar`）。

### 第四步：拉取 Docker 镜像
在 `deploy` 目录下预先拉取运行所需容器镜像：
```powershell
cd deploy
docker compose pull
```

### 第五步：生成桌面控制面板
在项目根目录双击执行：
```cmd
Install-Shortcuts.bat
```
执行后，您的桌面上将自动生成 `HeyGem-常用启动器` 文件夹，内置 4 个分类的快捷操作工具箱。

### 第六步：一键启动
双击桌面或根目录的 `Start-HeyGem.bat`，脚本将自动完成：
1. 检测并启动 Docker 引擎；
2. 启动 Docker Compose 业务容器；
3. 检查当前 TTS 模式并拉起对应服务；
4. 启动后台队列守护进程与看门狗；
5. 自动唤起 `Duix.Avatar.exe` 客户端界面。

---

## 4. 日常使用与启动器说明

生成在桌面的 `HeyGem-常用启动器` 包含以下实用工具：

### 01-启动停止
- **HeyGem-启动.lnk**：全自动化链条启动，具备等待服务探活检查。
- **HeyGem-停止.lnk**：一键优雅清理并停止后台容器、调度器与守护进程。

### 02-队列控制
- **查看实时运行状态.lnk**：打开终端控制台，实时查看 Docker 状态、IndexTTS2 探活、当前排队任务数、正在渲染的任务及最新日志。
- **暂停任务排队.lnk**：打上暂停标记，调度器不再派发新任务，便于维护（正在跑的任务会正常完成）。
- **恢复任务排队.lnk**：清除暂停标记，排队任务继续按序渲染。

### 03-TTS切换
- **切换为IndexTTS2.lnk**：将合成引擎切换至本地 IndexTTS2 高质量零样本克隆。
- **切回原版FishSpeech.lnk**：切回 Docker 容器内置的原生 Fish-Speech。

### 04-维护工具
- **批量导出视频.lnk**：自动比对数据库，将渲染完成的高清视频统一规整命名并复制到交付目录。
- **清理临时缓存.lnk**：安全删除孤儿 WAV、清除 12 小时前过期的渲染残留目录、修剪超大日志。
- **批量清理作品.lnk**：批量删除历史测试作品记录，严格保护模特底模不被误删。

---

## 5. 高级配置与环境变量覆盖

系统默认使用智能路径探测。如果在新电脑上盘符不同（例如只有 C 盘或安装在 F 盘），无需修改任何代码，只需设置对应的系统环境变量即可（或参考 `heygem.config.example.json`）：

| 环境变量名 | 默认探测逻辑 | 说明 |
| :--- | :--- | :--- |
| `HEYGEM_DB_OVERRIDE` | `%APPDATA%\Duix.Avatar\biz.db` | 客户端 SQLite 数据库路径 |
| `HEYGEM_OUTPUT_ROOT` | `D:\duix_avatar_data\face2face\temp` -> `C:\...` | Docker 渲染音视频工作目录 |
| `HEYGEM_DELIVERY_ROOT` | `E:\数字人视频文件` -> `D:\...` -> `桌面\数字人视频文件` | 成品自动交付导出归档目录 |
| `HEYGEM_VOICE_ROOT` | `D:\duix_avatar_data\voice\data` -> `C:\...` | 模特音频与参考音存放目录 |
| `HEYGEM_INDEXTTS2_ROOT` | `D:\HeyGem\tts-story-f5` | IndexTTS2 项目代码根目录 |
| `HEYGEM_ADAPTER_PYTHON` | `%HEYGEM_INDEXTTS2_ROOT%\venv\Scripts\python.exe` | 运行 IndexTTS2 服务的 Python 环境 |
| `HEYGEM_CLIENT_EXE` | `C:\Program Files\Duix.Avatar\Duix.Avatar.exe` | 桌面端客户端可执行文件路径 |

---

## 6. 常见故障排查与运维

### Q1: 界面一直显示“排队中，请耐心等待”，但没有进度？
- **排查方法**：运行 `02-队列控制\查看实时运行状态.lnk`，或查看 `logs\heygem-queue-daemon.out.log`。
- **机制保障**：HeyGem-Enhanced 会自动接管卡顿任务。如果容器正在输出帧（av_transfer / avi write success），说明任务正在平稳渲染中，仅是前端进度条未刷新，切勿强制重启容器。

### Q2: 切换到 IndexTTS2 报 500 错误？
- 确认是否已部署并配置了 IndexTTS2 项目；
- 查看 `logs\heygem-index-tts2.err.log`；
- 若暂时不需要 IndexTTS2，点击 `03-TTS切换\切回原版FishSpeech.lnk` 即可无缝切回原生合成。

### Q3: 磁盘空间告急？
- 双击 `04-维护工具\清理临时缓存.lnk`，将自动安全释放过期临时渲染目录与无主中间音频。
