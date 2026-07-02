# 网页逆向工作指令

本项目在开发自动注册流程时,需要对目标网站做抓包与 JS 逆向分析,以下工具可用。

## 可用工具(均已安装)

### MCP 工具(直接调用)
- **anything-analyzer** — 流量抓包 + AI 协议逆向(网页/App/终端/手机)。端口 23816,需 Anything Analyzer 应用在运行并开启 MCP 服务端。工具:create_session / start_capture / navigate / get_requests / filter_requests / run_analysis 等 30 个。
- **js-reverse** — JavaScript 逆向专用 MCP(CDP 断点、脚本分析、网络/WS 分析、反检测)。npx 启动,需 Chrome。22 个工具。

### 命令行工具(已装,走 PATH)
- **frida** — 动态插桩/Hook(17.9.4)。`frida --version` 验证。设备端需另跑 frida-server。
- **radare2** — 静态二进制分析(解包/反汇编/补丁,6.1.8)。装在 `D:\radare2\radare2-6.1.8-w64\bin`,已加入用户 PATH。`r2 -v` 验证。

### 逆向 Skill(方法论参考,需要时去读)

以下 skill 文件在 `C:\Users\15692\.claude\skills\<名称>\SKILL.md` 及其 references 子目录。遇到对应场景时,**先读对应 SKILL.md 再动手**:

| 场景 | 去读 | 路径 |
|------|------|------|
| 通用逆向方法论(混淆/壳/字节码/反调试/固件/CTF) | reverse-engineering | `~/.claude/skills/reverse-engineering/SKILL.md` |
| radare2 / rabin2 命令行二进制分析 | radare2 | `~/.claude/skills/radare2/SKILL.md` |
| IDA Pro / idalib 逆向(配合 ida-multi-mcp) | ida-reverse | `~/.claude/skills/ida-reverse/SKILL.md` |
| Android APK 解包/反编译/smali/重打包/Frida Hook | apk-reverse | `~/.claude/skills/apk-reverse/SKILL.md` |
| 前端 JS 逆向(签名链定位/补环境/运行时采样) | mcp-js-reverse-playbook | `~/.claude/skills/mcp-js-reverse-playbook/SKILL.md` |

## 工作原则

1. **先确认工具可用再动手**:radare2 用 `r2 -v`、frida 用 `frida --version`、anything-analyzer 看应用是否在跑(端口 23816)。
2. **场景匹配 skill**:用户要分析 exe/dll/so → 读 radare2 skill;要 JS 逆向 → 读 mcp-js-reverse-playbook;APK → 读 apk-reverse;不确定 → 先读 reverse-engineering。
3. **MCP 优先于手动**:抓包/JS 逆向优先用 anything-analyzer 和 js-reverse 这两个 MCP,它们把 DevTools 封装成适合连续推理的工具。
4. **radare2 已装**:在 `D:\radare2\radare2-6.1.8-w64\bin`,已入 PATH。新开终端 `r2` 即可用;当前终端若找不到,用全路径或重开终端刷新 PATH。
