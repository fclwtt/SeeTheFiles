# SeeTheFiles

将任意文件夹渲染为**美观的思维导图式结构视图**的 Python CLI 工具。基于已定稿 SPEC 实现（CAP-1…CAP-5）。

## HOW 决策（实现前已锁定）

| # | 决策 | 理由 |
|---|------|------|
| **Q1** | **自包含 HTML（浏览器弹窗）** 而非原生窗口 | 满足 SPEC 硬约束"优先标准库、少第三方依赖"（原生 PyQt 破约束，Tkinter 达不到 XMind/SVG 美观度）。纯 HTML/CSS/JS 零依赖，`file://` 打开即弹窗；扫描归 Python、渲染归浏览器，天然"数万文件不卡 UI"。 |
| **Q2** | **文件内容内嵌进 HTML（不起 server）** | 内嵌方案天然满足 SPEC 安全边界：无端口、无 127.0.0.1、进程退出即销毁（临时 HTML 每次运行重新生成）、零磁盘泄露。超大文件仅内嵌截断片段 + 友好提示。 |
| **Q3** | 文本判定 = 扩展名白名单 + 大小嗅探（读前 8KB 检测非打印字节）；单文件预览上限 `max_preview_bytes = 512 KB` | 兼顾识别准确率与体积控制。 |
| **Q4** | 提供 `install.py` / `uninstall.py` + PyInstaller 打包的 **三个 exe**（Install / Uninstall / Run）；右键 command 指向自带 Python 运行时的 `SeeTheFiles-Run.exe "%1"`，**目标机零 Python 依赖**；默认用户级（HKCU）无感安装 | 真正的"一键"：用户双击 Install.exe 即生效，不碰命令行、不编辑任何文件、不需本机装 Python |
| **Q5** | 默认忽略 `.git/node_modules/__pycache__` 等（可在 `config.toml` 配置追加）；`max_depth=12`、`max_files=100000`（锚定"数万文件"基准：本机 5 万文件目录验证）；symlink 环用 `realpath` 递归栈防护 | 使 CAP-4 可测、可复现。 |

## 范围（默认不含，除非另行指定）
导出（PNG/SVG/HTML）、macOS/Linux 右键注册 —— **不在本次范围**。

## 使用

### 一键安装（推荐，普通用户）

1. 拿到打包好的 `dist/` 文件夹（含三个 exe + `config.toml`）。
2. **双击 `SeeTheFiles-Install.exe`** → 右键菜单注册完成（默认当前用户，无需管理员、无 UAC 弹窗、无需本机装 Python）。
3. 在任意文件夹上右键 → 出现"用 SeeTheFiles 查看结构" → 点击即弹出视图。
4. 卸载：双击 `SeeTheFiles-Uninstall.exe` 即可，干净清理注册表。

> 机器级（所有用户）安装：右键 `SeeTheFiles-Install.exe` → "以管理员身份运行"，无需修改脚本。安装器默认只做用户级注册，刻意不请求提权，以避免 UAC 把菜单写进管理员账户而当前用户看不到。

> `SeeTheFiles-Run.exe` 是被右键命令实际调用的运行器，自带 Python 运行时，**目标机器完全不需要安装 Python**。

### 从源码运行 / 构建

```bash
# 直接以 CLI 使用（需要本机 Python 3.11+）
python see_the_files.py /path/to/folder

# 注册表注册（源码方式，需 Windows）
python install.py            # 默认当前用户（HKCU），无感
python install.py --machine  # 所有用户（HKLM，需管理员）
python uninstall.py          # 卸载

# 打包成 exe（需 pip install pyinstaller）
python build_exes.py         # 产物在 dist/
```

### 常用选项（CLI / Run.exe 均支持）

```bash
python see_the_files.py <dir> --max-depth 8 --max-files 200000
python see_the_files.py <dir> --ignore dist --ignore .cache
python see_the_files.py <dir> --no-open --output view.html   # 仅生成不打开
python see_the_files.py <dir> --print-json                    # 仅打印树 JSON
```

## 验证结果（对照 SPEC success signal）

| CAP | 验证方式 | 结果 |
|-----|----------|------|
| CAP-1 右键输入 + 重复触发 | 模拟 3 次并发触发 + 2 次自动临时文件名调用，输出文件唯一、互不覆盖 | ✅ 通过 |
| CAP-2 思维导图可视化 | 生成 HTML 含 `<ul>` 树、折叠/展开 `toggle()`、过滤 `filterTree()`、配色/图标/连线/动画 CSS | ✅ 结构完整 |
| CAP-3 文件预览 | 文本内嵌即时预览；二进制/超大文件友好提示不崩溃；超 `max_embed_files` 提示"预览已禁用" | ✅ 通过 |
| CAP-4 大目录防护 | 5 万文件目录 0.28s 扫描完、不截断、不崩溃；symbolic 链接环（含链式）检出并跳过、无栈溢出；`max_files`/`max_depth` 上限生效 | ✅ 通过 |
| CAP-5 一键安装 | `build_exes.py` 产出三个 exe；双击 Install.exe 即注册，command 指向自带运行时的 Run.exe，目标机零 Python 依赖 | ✅ 已修复实测（注册表路径 `Software\Classes\Directory\shell`、exe 真实路径定位、无 UAC 误写） |

## 文件结构

本工具已收敛至 `seethes/` 子目录（与 BMad 工作区隔离），以下路径均相对于 `seethes/`：

- `see_the_files.py` — 核心：配置加载、扫描器（CAP-4）、分类、HTML 渲染（CAP-2/3）、CLI（CAP-1）
- `config.toml` — 忽略清单、深度/数量/预览上限、文本扩展名、主题色
- `install.py` / `uninstall.py` — 右键菜单注册（CAP-5，Windows 源码版）
- `run_app.py` — Run.exe 入口（被右键命令调用，无窗口）
- `build_exes.py` — PyInstaller 打包脚本，产出 Install/Uninstall/Run 三个 exe
- `dist/` — 构建产物：`SeeTheFiles-Install.exe` / `SeeTheFiles-Uninstall.exe` / `SeeTheFiles-Run.exe` / `config.toml`（分发此目录即可）

## 依赖
仅 Python 标准库（`argparse / json / os / pathlib / tempfile / webbrowser / html`）。3.11+ 用 `tomllib` 解析配置；低版本有内建 mini TOML 回退。
