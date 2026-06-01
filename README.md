# Book TTS

将电子书（EPUB、MOBI、Markdown）转换为有声书，支持 M4B 格式输出。

## 功能特性

- **多格式输入**：EPUB、MOBI/AZW、Markdown
- **多种 TTS 引擎**：MiMo TTS（OpenAI 兼容 API）、Edge TTS（免费回退）
- **智能文本预处理**：数字转中文、CJK-Latin 间距处理、标点规范化、叠字符号归一化
- **章节感知**：自动章节检测、前言后记过滤（目录、版权、封面、扉页等）
- **M4B 有声书**：带章节标记、封面嵌入、音频音量归一化
- **断点续传**：中断后自动跳过已完成章节
- **并发合成**：多线程段落合成，频率限制（90 RPM）
- **Web GUI**：Gradio 中文界面
- **两种 EPUB 解析器**：标准导航解析 / 纯 HTML 解析
- **Dry Run 模式**：不调用 TTS，预览解析结果
- **语音试听**：转换前试听效果
- **费用估算**：根据字数估算 API 调用成本

## 安装

```bash
# 基本安装
pip install book-tts

# 包含 WeTextProcessing（更好的中文数字规范化）
pip install book-tts[wetext]

# 包含 Edge TTS（免费回退方案）
pip install book-tts[edge]

# 全部可选依赖
pip install book-tts[wetext,edge]
```

### 前置依赖

- Python 3.10+
- FFmpeg（音频处理必需）
  - Ubuntu/Debian：`sudo apt install ffmpeg`
  - macOS：`brew install ffmpeg`
  - Windows：从 https://ffmpeg.org/download.html 下载

## 快速开始

### CLI 用法

```bash
# 设置 API Key 后转换
export MIMO_TTS_API_KEYS="your-api-key"
book-tts mybook.epub

# 多个 API Key 负载均衡
export MIMO_TTS_API_KEYS="key1,key2,key3"
book-tts mybook.epub

# 转换为 M4B 格式（默认）
book-tts mybook.epub --format m4b

# 断点续传
book-tts mybook.epub --resume

# 试运行（不调用 TTS，生成文本预览）
book-tts mybook.epub --dry-run

# 启动 Web GUI
book-tts --gui

# 调试模式
book-tts mybook.epub --verbose
```

### GUI 用法

```bash
book-tts --gui
```

浏览器打开 http://localhost:7860：

1. 上传电子书（EPUB、MOBI、AZW、Markdown）
2. 选择解析器（标准 / 纯 HTML）
3. 选择要转换的章节
4. 配置 TTS（音色、风格、API Key）
5. 可选：试听语音、估算费用
6. 选择输出格式和比特率
7. 点击"开始转换"，实时查看进度
8. 通过 SMB 访问输出目录获取文件

## 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MIMO_TTS_API_KEYS` | 逗号分隔的 API Key | （无） |

### CLI 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | 输入文件或目录 | （必填） |
| `--output` | 输出目录 | `audiobook_output` |
| `--voice` | TTS 音色名称 | `冰糖` |
| `--style` | TTS 风格描述 | （见下方） |
| `--base-url` | TTS API 地址 | `https://api.xiaomimimo.com/v1` |
| `--api-key` | 单个 API Key | （无） |
| `--api-keys` | 多个 API Key（空格分隔） | （无） |
| `--format` | 输出格式：mp3、m4b | `m4b` |
| `--resume` | 断点续传 | false |
| `--dry-run` | 预览文本，不调用 TTS | false |
| `--gui` | 启动 Web 界面 | false |
| `--verbose` / `-v` | 调试日志 | false |

### 默认语音风格

> 温柔、甜美、富有感情的女性声音，语速适中，吐字清晰，带有自然的抑扬顿挫，适合长时间有声书朗读。

### 可用音色

音色名称直接传递给 MiMo TTS API。使用 `冰糖` 或服务商提供的其他音色标识。

## 架构

```
book_tts/
├── main.py               # CLI 入口
├── pipeline.py           # 转换流程（解析→合成→合并）
├── config.py             # 常量和默认值
├── models.py             # 数据模型
├── markup.py             # SML 标记注入
├── parsers/
│   ├── base.py           # 抽象解析器
│   ├── epub_parser.py    # EPUB 解析（含 HTML 解析器）
│   ├── mobi_parser.py    # MOBI/AZW 解析
│   ├── markdown_parser.py
│   ├── text_cleaner.py   # 文本规范化
│   └── number_converter.py
├── tts/
│   ├── client.py         # MiMo TTS HTTP 客户端
│   ├── edge_client.py    # Edge TTS 回退
│   ├── synthesizer.py    # 段落级合成
│   ├── rate_limiter.py   # 频率限制器
│   └── sml.py            # 语音标记
├── audio/
│   ├── merger.py         # MP3 章节合并
│   └── m4b_builder.py    # M4B 有声书构建
├── gui/
│   ├── app.py            # Gradio Web 界面
│   ├── components.py     # UI 组件
│   └── state.py          # 转换状态管理
└── utils/
    ├── progress.py       # 线程安全进度跟踪
    ├── checkpoint.py     # 断点续传
    ├── history.py        # 音色/风格使用历史
    ├── preferences.py    # 用户偏好持久化
    └── file_utils.py     # 文件系统工具
```

### 流水线流程

1. **解析**：提取电子书文本，检测章节，过滤前言后记
2. **清洗**：数字标准化、CJK 间距处理、标点规范化、注入 SML 标记
3. **合成**：通过 TTS API 将段落转为音频，并发处理
4. **合并**：段落音频拼接为章节 → M4B/MP3，嵌入封面，音量归一化

## 开发

```bash
# 克隆仓库
git clone https://github.com/yourusername/book_tts.git
cd book_tts

# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/ -v

# 代码检查
ruff check book_tts/
mypy book_tts/

# 格式化
black book_tts/
```

## 许可协议

MIT License
