# HeyGem-Enhanced · HeyGem Studio 数字人一体化工作台与自动化生产中枢

基于官方 **Duix.Avatar (HeyGem)** 深度二次开发与生产级重构的本地数字人创作中心。内置全新 **HeyGem Studio 一体化 Web 工作台**，将数字人形象管理、**IndexTTS-2 / IndexTTS-2.5** 语音全参数调控试听、实时帧级进度监控、任务随时中断与自愈恢复、作品分类下载以及标准化 **AI 自动化 REST API** 全部融合在一个无黑框浏览器界面中。

---

## 目录
- [1. 核心升级与痛点闭环](#1-核心升级与痛点闭环)
- [2. HeyGem Studio 一体化工作台使用指南](#2-heygem-studio-一体化工作台使用指南)
- [3. 无黑框启动与日常运维](#3-无黑框启动与日常运维)
- [4. IndexTTS-2 / 2.5 语音深度调控说明](#4-indextts-2--25-语音深度调控说明)
- [5. 标准化自动化 AI 接口 (REST API)](#5-标准化自动化-ai-接口-rest-api)
- [6. 项目架构与目录划分](#6-项目架构与目录划分)
- [7. 新电脑部署与环境变量覆盖](#7-新电脑部署与环境变量覆盖)
- [8. 排障与一键自愈指南](#8-排障与一键自愈指南)

---

## 1. 核心升级与痛点闭环

针对原版 Duix.Avatar 启动弹黑框、配音与数字人割裂、进度不透明、任务易假死且无自动化接口等问题，本项目已完成系统性改造：

| 痛点问题 | 原版表现 | HeyGem-Enhanced / Studio 解决方案 |
| :--- | :--- | :--- |
| **启动跳多个黑框命令窗口** | 启动时弹出多个 PowerShell/CMD 窗口，体验割裂。 | 提供 **`Start-Studio.vbs`** / **`Stop-Studio.vbs`** 全静默无黑框启动停止，并改造 `Start-HeyGem.bat` 与桌面快捷方式默认无框拉起。 |
| **配音与数字人页面割裂** | IndexTTS2 无法在数字人界面调参试听，只能跨窗口折腾。 | **融合创作工作台**：同一页面选数字人、调 IndexTTS-2/2.5 全参数、先合成试听音频、满意后一键直出视频（免重复合成）或批量音频直驱。 |
| **生成进度不透明、不知是否报错** | 长期卡在 10%/20%，容器崩溃或断连无感知。 | **真实帧级遥测 + 全链路可观测**：实时解析容器 25/30/60fps 写帧数、FPS 与 ETA；顶部健康灯实时监控 Docker/TTS/队列/GPU/磁盘；后台断连自动红灯告警并自动重连。 |
| **无法安全中断或卡死难恢复** | 删任务后后台仍在空烧显存，或停服后失去自愈能力。 | **非破坏性实时中断**：排队立即取消、配音立即强杀 Worker 释放显存、渲染安全重启容器；点击「一键启动 / 自愈恢复」自动尝试拉起 Docker Desktop、容器、TTS 适配器、队列调度器及 Guard 守护进程。 |
| **缺少分类与源文件定位** | 成片混在临时目录，找文件、改名、归类麻烦。 | **作品与音频分类库**：自动交付至 `E:\数字人视频文件/<分类名>/`，支持在线预览、一键下载、打开本地源文件目录、重命名、改分类自动移文件、参数一键复用。 |
| **手工点击低效、无法对接 AI** | 只能人工在 Electron 客户端逐条点击提交。 | **标准化 REST API + Swagger `/docs`**：支持单条/批量原子提交（前置校验文件完整性）、轮询进度、中断重试、成片下载与 Webhook 回调。 |

---

## 2. HeyGem Studio 一体化工作台使用指南

启动后浏览器自动打开 **`http://127.0.0.1:8390`**（支持通过环境变量 `HEYGEM_STUDIO_PORT` 调整端口，界面接口地址自动跟随当前访问地址）：

1. **🎬 快速创作（主工作台）**
   - **左栏（数字人形象）**：按分组/关键词筛选模特、预览底模视频、缺失底模一键重新关联、导入新模特（支持网页上传或粘贴本机路径秒复制，自动提取参考音色）。
   - **中栏（双模创作）**：
     - **IndexTTS2 文本配音**：选择参考音色（默认跟随所选模特，可自由换声/上传新音色）、输入口播文案、展开调节 IndexTTS-2/2.5 参数或套用/保存/删除预设；可「先合成试听音频」再驱动，也可「一键直出数字人视频」。
     - **导入音频直驱**：支持拖拽/多选上传音频、**粘贴本机音频路径快速导入**（自动清洗 Windows 复制路径自带引号，支持多行批量粘贴），勾选音频库素材一键批量入队。
   - **右栏（实时执行监视器）**：显示五阶段流水线（排队 → 语音 → 唇形 → 封装 → 交付）、已写帧数/总帧数、FPS、ETA、心跳状态与一键「中断任务」。
2. **📊 任务监控**：按状态筛选全部/进行中/已完成/报错失败/已中断任务；失败任务直接展示明确错误码与原始日志折叠面板，支持重命名、中断、重试、预览、下载与定位目录。
3. **🗂️ 作品与音频库**：按自定义分类管理已完成视频成品与音频素材；支持作品/音频重命名、拖拽改分类（成品自动同步移入对应交付子目录）、查看所有源文件路径、一键「复用参数」（同步还原模特、音色、文案与 TTS 参数）。
4. **👤 模特与音色**：集中管理数字人底模（预览、重命名、分组、替换/重新关联底模、删除）与参考音色库（在线试听、上传独立参考音）。
5. **🤖 自动化 AI 接口**：内置自动填入当前真实模特 ID 的 Python 调用示例、在线联调测试台以及 Swagger 交互式 API 文档入口（`/docs`）。

---

## 3. 无黑框启动与日常运维

### 推荐日常入口（全程零黑框）
- **一键启动**：双击根目录 **`Start-Studio.vbs`**（或 **`Start-HeyGem.bat`** / 桌面 `HeyGem-常用启动器\01-启动停止\HeyGem-启动.lnk`）。
  - 优先快速拉起 HeyGem Studio Web 服务并打开浏览器，后台静默按序启动 IndexTTS2 适配器（18180）、Docker Desktop 及渲染容器（8383）、队列调度器与 Guard 守护进程。
- **一键停止**：双击根目录 **`Stop-Studio.vbs`**（或 **`Stop-HeyGem.bat`**），安全停止守护进程、队列、IndexTTS2、Docker 容器与 Studio 服务。
- **网页端启停自愈**：在浏览器顶部栏直接点击「▶ 一键启动服务」「🩺 自愈恢复」「⏸ 暂停队列」「📜 实时日志」，或在左下角点击「⏹ 停止后台服务」（不关闭网页，随时可在网页再次一键拉起）。
- **如需兼容旧版 Electron 客户端**：可运行 `powershell -ExecutionPolicy Bypass -File scripts\start-heygem.ps1 -LegacyClient`。

### 桌面工具箱生成
双击根目录 **`Install-Shortcuts.bat`**，将在桌面生成 `HeyGem-常用启动器`，包含启停、队列控制、TTS 模式切换（IndexTTS2 / Fish-Speech）及缓存清理快捷方式。

---

## 4. IndexTTS-2 / 2.5 语音深度调控说明

本地适配服务运行在 `http://127.0.0.1:18180`（由独立推理子进程执行，合成完成后立即释放 GPU 显存，与 Docker 渲染互不抢占显存）：

- **模型版本**：自动探测本机权重，支持在 `IndexTTS-2`（经典高质量）与 `IndexTTS-2.5`（多语种 ZH/EN/JA/ES/AR 原生变速）间切换。
- **4 种情感控制模式**：
  1. `same_as_speaker`：跟随参考音色自然语气；
  2. `emo_vector`：8 维情感滑块（喜/怒/哀/惧/厌恶/低落/惊喜/平静），总权重超过 `0.80` 时自动柔和归一化防失真；
  3. `emo_audio`：上传或指定一段带目标情绪的情感参考音频；
  4. `emo_text`：自然语言描述情绪（启用 Qwen 情感推理）。
- **高级采样与长文加速**：支持调节 `speed`（语速）、`diffusion_steps`（扩散步数）、`num_beams`（束搜索）、`temperature`、`top_p`、`top_k`、`repetition_penalty`、`interval_silence`（句间停顿 ms）、`chunk_size`（分段字数）与长文自动提速开关。
- **预设与全局默认**：可将任意调好的参数「💾 存为预设」、随时切换或「🗑 删预设」，也可点击「设默认」写入 `adapter/config.json` 供后续任务默认继承。

---

## 5. 标准化自动化 AI 接口 (REST API)

服务默认监听 `http://127.0.0.1:8390`，完整 OpenAPI 交互文档见 **`/docs`**。如需启用接口鉴权，设置环境变量 `HEYGEM_STUDIO_API_KEY`，调用时在请求头携带 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。

### 核心接口一览
| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/api/v1/ping` | 极速存活探测 |
| `GET` | `/api/v1/health` | 全系统健康检查（Docker / IndexTTS2 / 队列与 Guard / GPU / 磁盘 / 告警） |
| `GET` | `/api/v1/models` | 获取可用数字人模特列表与底模完整性状态 |
| `POST` | `/api/v1/tts/synthesize` | 单独调用 IndexTTS2 合成音频并存入音频库 |
| `POST` | `/api/v1/audios/import-local` | 通过本机文件路径批量快速导入音频（自动清洗引号） |
| `POST` | `/api/v1/jobs` | 创建单条数字人任务（前置校验底模、音色与情感音频文件） |
| `POST` | `/api/v1/jobs/batch` | **原子批量创建**数字人任务（先全量校验再入库，任一条失败零残留） |
| `GET` | `/api/v1/jobs/{id}` | 查询实时阶段、帧进度%、FPS、ETA、报错详情与本地源文件路径 |
| `PATCH` | `/api/v1/jobs/{id}` | 重命名作品或修改作品分类（同步移动成片子目录） |
| `POST` | `/api/v1/jobs/{id}/cancel` | 实时中断排队/配音/渲染中的任务 |
| `POST` | `/api/v1/jobs/{id}/retry` | 失败或已中断任务一键重试 |
| `GET` | `/api/v1/jobs/{id}/download` | 下载最终成片 MP4 |
| `POST` | `/api/v1/system/recover` | 一键自愈拉起 Docker / 容器 / TTS / 队列 / Guard 并重置僵尸挂起任务 |

### Python 自动化调用示例
```python
import requests, time

BASE = "http://127.0.0.1:8390/api/v1"

# 1) 提交文案 + IndexTTS2 参数任务（支持自动创建分类与 Webhook 回调）
resp = requests.post(f"{BASE}/jobs", json={
    "model_id": 1,
    "name": "自动口播示例",
    "category_name": "日常口播",
    "text": "大家好，这是通过自动化接口生成的数字人视频。",
    "tts_params": {
        "model_version": "IndexTTS-2",
        "speed": 1.02,
        "diffusion_steps": 25,
        "num_beams": 2,
        "emo_mode": "emo_vector",
        "emo_alpha": 0.65,
        "emo_vector": [0.4, 0, 0, 0, 0, 0, 0.2, 0]
    },
    # 可选：完成后自动回调通知（事件：job.completed / job.failed）
    # "webhook_url": "http://127.0.0.1:9000/callback"
})
resp.raise_for_status()
job_id = resp.json()["job_id"]

# 2) 轮询实时进度与结果
while True:
    st = requests.get(f"{BASE}/jobs/{job_id}").json()["item"]
    print(f"[{st['status']}] {st['progress_float']}% | {st['stage_label']}")
    if st["status"] in ("success", "failed", "cancelled"):
        break
    time.sleep(5)

if st["status"] == "success":
    print("成片路径:", st["paths"]["delivered_path"])
    print("下载链接:", f"{BASE}/jobs/{job_id}/download")
```

---

## 6. 项目架构与目录划分

```text
HeyGem-Enhanced/
├── Start-Studio.vbs                  # [推荐入口] 零黑框静默启动完整 Studio 环境
├── Stop-Studio.vbs                   # [推荐入口] 零黑框静默优雅停止服务
├── Start-HeyGem.bat / Stop-HeyGem.bat# 批处理入口（已转发至无框 VBS）
├── Install-Shortcuts.bat             # 一键在桌面创建分类控制启动器
├── tts-mode.txt                      # 当前 TTS 模式 (index-tts2 / fish-speech)
│
├── studio/                           # HeyGem Studio 统一中枢与 Web 工作台
│   ├── server.py                     # FastAPI 后端、媒体流、自动化 AI REST API
│   ├── db.py                         # SQLite 扩展表初始化与媒体探测工具
│   ├── system_ctrl.py                # 健康聚合、Docker Desktop 自动拉起与自愈控制
│   └── static/                       # 原生轻量前端 (index.html / app.js / styles.css)
│
├── adapter/                          # IndexTTS-2 / 2.5 本地语音适配层 (端口 18180)
│   ├── heygem_index_tts2_adapter.py  # Flask 适配服务（锁隔离、实时状态、精准中断）
│   ├── indextts2_studio_worker.py    # 独立 GPU 推理子进程（用完即放显存）
│   └── config.json                   # 全局默认语音推理参数
│
├── core/                             # 核心队列调度与守护自愈引擎
│   ├── heygem_queue_runner.py        # 队列串行调度、60fps 帧遥测、重试上限、自动交付与 Webhook
│   └── heygem-queue-guard.ps1        # 后台守护进程（单实例保活、断服自动拉起）
│
├── scripts/                          # 启停编排、底模修复、缓存清理等维护脚本
├── deploy/                           # Docker Compose 配置（标准/精简/5090/Linux）
├── launchers/                        # 桌面启动器对应脚本
└── tests/                            # 自动化单元回归测试与真实浏览器端到端验收脚本
```

---

## 7. 新电脑部署与环境变量覆盖

1. **环境准备**：Windows 10/11 64 位 + NVIDIA RTX 显卡（推荐 12GB+ 显存），安装 Python 3.10~3.12 与 Docker Desktop（启用 WSL2 + NVIDIA Container Toolkit）。
2. **安装依赖**：
   - Studio Web 中枢需要 `fastapi`、`uvicorn`、`python-multipart`（已包含于常规 Python 环境或 `pip install fastapi uvicorn python-multipart`）。
   - IndexTTS2 适配器依赖：`pip install -r adapter\requirements.txt`（若本机未部署 IndexTTS2 权重，启动脚本自动平滑降级为官方 Docker Fish-Speech）。
3. **拉取渲染镜像**：在 `deploy/` 目录执行 `docker compose pull`。
4. **一键启动**：双击 `Start-Studio.vbs`，等待浏览器打开工作台并观察顶部健康指示灯全部变绿即可开始使用。

### 常用环境变量（可选覆盖）
| 环境变量名 | 默认探测逻辑 | 说明 |
| :--- | :--- | :--- |
| `HEYGEM_STUDIO_HOST` / `PORT` | `127.0.0.1` / `8390` | Studio Web 工作台监听地址与端口 |
| `HEYGEM_STUDIO_API_KEY` | 空（不鉴权） | 开启后要求 REST API 请求携带 `X-API-Key` 或 `Bearer` Token |
| `DUIX_DATA_DIR` | `D:/duix_avatar_data` | Docker 容器共享数据根目录 |
| `HEYGEM_DELIVERY_ROOT` | `E:\数字人视频文件` -> `D:\...` | 成片自动归档交付目录 |
| `HEYGEM_INDEXTTS2_ROOT` | `D:\HeyGem\tts-story-f5` | IndexTTS2 引擎代码与权重根目录 |
| `DOCKER_DESKTOP_PATH` | `C:\Program Files\Docker\Docker\Docker Desktop.exe` | 网页一键启动时自动拉起 Docker Desktop 的路径 |
| `HEYGEM_DB_OVERRIDE` | `%APPDATA%\Duix.Avatar\biz.db` | SQLite 业务数据库路径覆盖 |

---

## 8. 排障与一键自愈指南

- **顶部出现红色横幅「与 HeyGem Studio 后台服务连接中断」**：说明本机 8390 后台进程已停止或正在重启。双击 `Start-Studio.vbs` 拉起服务后，页面会自动重连并恢复绿色健康灯（也可点击横幅右侧「🔄 立即重连」）。
- **渲染引擎 / IndexTTS2 / 队列显示红色未启动**：点击顶部「🩺 自愈恢复」或「▶ 一键启动服务」，系统会自动检测并拉起 Docker Desktop、渲染容器、IndexTTS2 适配器、队列调度器以及 Guard 守护进程，并在弹窗展示逐项诊断结果。
- **渲染任务报错或无响应**：打开「📊 任务监控」展开对应任务的「查看原始错误堆栈/日志」，或点击顶部「📜 实时日志」切换查看调度队列、IndexTTS2、Docker 视频渲染与守护日志。若渲染容器疑似显存死锁，可在自愈弹窗中点击「强制重启视频渲染容器」。
- **模特提示「底模视频缺失」**：前往「👤 模特与音色」页面，点击对应模特卡片的「🔗 重新关联底模」，上传或粘贴原拍摄视频的本机路径即可秒恢复。
- **磁盘空间不足**：使用桌面工具箱 `04-维护工具\清理临时缓存.lnk` 或运行 `python scripts/cleanup_heygem_cache.py` 安全清理过期中间文件与超大日志。
