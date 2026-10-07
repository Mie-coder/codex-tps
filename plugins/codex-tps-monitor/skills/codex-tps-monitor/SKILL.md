---
name: codex-tps-monitor
description: Inspect per-turn TPS, tokens-per-second and TPS details from the official Stop Hook; manage optional macOS reply footer display when explicitly requested.
---

# Codex TPS

Resolve the plugin root as two directories above this skill folder. Read the repository README for installation when available.

The official Stop Hook runs automatically after each completed response ONLY when the plugin is enabled, the hooks feature is enabled and the current TPS Hook definition is trusted. Normal Codex startup loads it. Installing a plugin alone does not grant Hook trust. Never bypass global trust or trust unrelated hooks.

Inspect saved statistics with `python3 scripts/stop_hook.py --status` from the plugin root. Saved statistics alone do not prove that the current Hook definition is active. The native reply action “钩子统计信息” shows run history; expand Stop and find “TPS 提示详情”. Verify a newly completed real turn after configuration loads.

The optional macOS footer needs the existing monitor and a loopback debug endpoint. It is not drawn by Stop Hook. When the user requests this display, run `python3 scripts/control.py auto-install`; this installs the service and generates `~/Applications/Codex TPS.app`. After active work finishes, the user can quit the client normally and open that launcher. Do not interrupt an active chat or restart the client without explicit authorization. Ordinary Codex startup without the debug endpoint leaves the footer waiting.

Inspect the monitor's own diagnostic status with `python3 scripts/control.py status`. A fresh receipt with exact active session match and nonzero rendered rows is runtime evidence; it is not a pixel screenshot. For explicitly authorized service changes use `start`, `stop` or `auto-remove`. Never change monitoring during an ordinary statistics query.

Both entry points reuse the transcript parser. Stop counts through script entry before task_complete; the footer uses completed-turn time. Effective TPS excludes identifiable tool wait but includes request and first-token wait; it is not pure server decode speed. Output already includes reasoning and generated tool calls. Never add reasoning twice, input, tool-return text or subagent output. Missing or inconsistent usage remains unavailable.

Read only the supplied log under the active CODEX_HOME; never save conversation text, modify transcripts/client bundles, upload logs, block the response or start another model turn for statistics. Do not claim a speed improvement from unmatched individual turns. A historical comparison requires date, model, reasoning effort, sample count and consistent timing definitions.
