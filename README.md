## Coach Agent (Flask Web App)

本项目包含一个简单的教练助手 Web 版，数据保存到本地 `coach_data.json`。

入口：`coach_agent.py`（多页面 + 双语切换）。

### 数据文件（重要）

- 本地运行会读写 `coach_data.json`（**不要提交到 GitHub**）。
- 仓库里提供了 `coach_data.sample.json`，首次运行可以复制一份：

```bash
cp coach_data.sample.json coach_data.json
```

### 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

### （可选）开启 AI 自动解析一句话课程记录

1) 复制环境变量文件：

```bash
cp .env.example .env
```

2) 编辑 `.env`，填入你的 key：

```text
OPENAI_API_KEY=你的key放这里
```

注意：`.env` 已在 `.gitignore` 中忽略，不要上传到 GitHub。

### 启动

```bash
python3 coach_agent.py
```

启动后打开：

- 主页：`http://127.0.0.1:5000/`
- 新增学员：`/students/new`
- 记录课程：`/lessons/new`
- 学员进度：`/students/<学员姓名>`
- 付款状态：`/payments`

