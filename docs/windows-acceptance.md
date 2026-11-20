# Windows 本机端到端验收

本次适配自动检测 macOS / Windows，无需手动切换系统配置。功能分支为 `codex/macos-windows-support`。请先在 Windows 验收；验收通过后再推送功能分支并创建 PR，不直接推送 main。

## 1. 启动与环境

在仓库根目录打开 PowerShell：

```powershell
python --version
python -m gazevoice
```

- Python 需要 3.12 或更新版本。若本机使用 Python Launcher，也可执行 `py -3.12 -m gazevoice`。
- 浏览器应打开控制中心。Routing 显示 `Windows` 和 `Whisper small (CPU int8)`，没有 MLX 引擎选择器。
- 首次打开不安装组件、不开麦克风或摄像头、不调用模型。
- Speech / Gaze / ML-Agents / Blender MCP server 在缺少 uv 时仍显示 Install；确认后先在 GazeVoice 数据目录的 tools/uv 中安装 uv，再继续组件安装，无需手动安装 uv 或重启。多个组件共用这一份 uv，安装失败时可重试。WinGet 来自 Windows 的 App Installer；安装 ffmpeg 或 CLI 后若仍显示缺失，重新打开 PowerShell 并重启控制中心，以更新 PATH。
- Claude Code / Codex 使用已有登录。原生 exe 与标准 npm 包入口受支持；自定义 .cmd/.bat 包装器会给出启动错误，请改用 exe，或在自定义 MCP 配置中填写 node.exe 和 JS 入口参数。
- 默认数据目录为 `%LOCALAPPDATA%/GazeVoice`。可用 `$env:GAZEVOICE_HOME = 'D:/GazeVoice Data'` 指定独立测试目录。不要把 macOS 的 venv 复制到 Windows。

## 2. Blender 主链路（必测）

1. 从控制中心安装或检测 Blender、官方 MCP、GazeVoice Bridge。Bridge 应为 **0.3.5**；更新后重启 Blender。便携版可设置 `$env:BLENDER_PATH = 'D:/Apps/Blender/blender.exe'` 后重启控制中心。
2. 检查 Launch 能打开 Blender；MCP 和 Bridge 的 Test 成功。测试在空白或可丢弃场景执行。
3. 安装 Speech-to-text。Test 用 Windows 自带语音生成音频，再由本地 faster-whisper 转录。首次安装会下载依赖和模型。
   - Windows 语音环境使用 uv 管理的独立 Python，避免沿用 Conda 的 C++ DLL。旧 Conda 环境会显示 Repair speech；修复只重建应用的语音环境，复用已缓存模型。
   - 如果出现退出码 3221225477（0xC0000005），表示原生运行库访问违规；不是转录结果为空。本机曾定位到 Miniconda 的 MSVCP140.dll。安装流程同时约束 faster-whisper 1.2.1 与 PyAV <19；Ready 检查会验证解码器版本及本地模型能否加载。
4. Routing 选中实际麦克风。Microphone Test 录制三秒并显示有效音量；权限关闭时应给出 Windows 设置路径。
5. 手动点击相应模型测试 / 连接，再 Start voice。选择一个立方体，按住 **Ctrl+Alt+Space** 说 “make this twice as tall”，松开后核对转录、指令和物体变化。
6. 说 undo / redo，确认一次撤销对应一次命令；按住说话时按 Esc 或切换窗口应取消。也测试侧栏 Talk 与控制中心 Hold to talk / 输入框。
7. Stop voice 后麦克风释放；重复 Start / Stop；控制中心 Ctrl+C 正常退出；重新启动可复用设置且不残留 worker。强制结束终端无法保证训练模型保存，请使用正常停止。

## 3. Unity 主链路（使用 Unity 时必测）

1. Routing 选择 Unity。Set up Unity 使用 WinGet / Unity Hub；也可以先在 Hub 安装所需版本。登录与 Personal 许可仍需在 Hub 完成。
2. 非默认安装可设置 `UNITY_HUB_PATH` 和 `UNITY_EDITOR_PATH` 为完整 exe 路径。项目目录可用 Choose 选择；建议包含空格和中文，验证路径正常。
3. 添加 / 更新 Bridge 至 **0.3.4**，等待 Unity 编译完成，Console 不应出现新增编译错误。
4. Bridge Test 验证 execute_csharp 可运行（自动查找 Unity 自带 dotnet.exe 与 csc.dll）。
5. Start voice，选中立方体，按 **Ctrl+Alt+Space** 发出相同编辑命令；核对状态、结果和一次 undo / redo。
6. 在 SceneView 使用 Mouse + Watch only，再用 On 验证 “this” 和空地 “here”；GameView 不应被误识别为 SceneView。
7. 如使用 ML-Agents：安装后启动短训练，Stop 应保存模型并释放端口；再次训练 / 恢复训练应成功。关闭控制中心前先正常 Stop。

## 4. 摄像头与隐私（可选）

- Camera Auto 跳过名称可识别的手机和虚拟摄像头；请确认选中的确为目标摄像头，同名设备建议明确选择。权限在 Windows Settings > Privacy & security > Camera，允许桌面应用访问。
- Calibrate 后核对有效精度，再使用 Test / Watch only。摄像头只在你开始相关功能后打开。
- Windows 无可靠屏幕物理尺寸时不启用头部转动补偿。先保持坐姿稳定，不把自动化测试视为真实视线精度验证。
- Windows Blender 摄像头视线映射目前要求单屏，或所有显示器使用相同缩放比例。混合 DPI 时拒绝猜测屏幕坐标并回退到选区；可改用 Mouse。校准与 Blender 放在同一屏幕，分别验证 100%、125%、150% 缩放。
- Pictures 默认关闭。需要时自行确认并运行清理检查；检查会调用 Claude。Windows 扫描 Local/Roaming AppData、临时目录及配置目录，不跟随内部 symlink/junction/reparse point。必查位置读取失败时不得记录通过。
- API provider 的密钥保存在 Windows Credential Manager，目标名 `GazeVoice/<provider-id>`。如使用 API，验证保存、重启读取、删除；不要在反馈中贴出密钥。

## 5. 验收记录

记录 Windows 版本、Python / Blender / Unity 版本、缩放比例、使用的 CLI / API 和模型。每个失败提供具体操作、预期/实际结果及控制中心相关日志（去掉密钥）。

- [ ] 自动识别系统、组件发现、安装与重启
- [ ] Blender 语音编辑、取消、撤销/重做、正常停止
- [ ] Unity 编译和语音链路（如使用）
- [ ] Mouse / 摄像头 gaze（如使用）
- [ ] API provider / ML-Agents / Pictures（如使用）

自动化回归：`python -B -m unittest discover -s tests -t . -q`。此命令不下载 ML 模型、不打开真实音视频设备。macOS/Windows CI 在功能分支推送或 PR 后执行；本机测试通过不代表 macOS 实机验收完成。
