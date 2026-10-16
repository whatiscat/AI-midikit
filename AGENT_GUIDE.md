# AGENT_GUIDE — 给智能体的操作手册

> 🤖 **本仓库由 AI 生成**：代码、测试与本文件均由 AI 编码智能体编写，人类负责需求与验收。

本文件面向"要动手改 MIDI 的智能体"。所有命令都可以直接执行，输出默认是 JSON，退出码见 README 第 9 节。

## 0. 三条铁律

1. **永远先看再改**：`info` / `notes` / `pianoroll` / `validate` 之前不要动文件。
2. **永远不要原地改**：用 `-o out.mid`；确实要覆盖时加 `--in-place --backup`。
3. **改完必验**：`validate` + `diff`，并在报告里说明"改了什么、影响多少音符"。

## 1. 决策表：我要做 X，用哪条命令？

| 目标 | 命令 |
| --- | --- |
| 了解文件 | `info F`（结构）/ `stats F`（音乐统计）/ `notes F`（音符） |
| 看清音乐 | `pianoroll F --text --height 24 --width 100` |
| 找特定事件 | `dump F --type note_on --tick-min 0 --tick-max 1920` |
| 判断文件是否合规/损坏 | `validate F --text` |
| 比较两个版本 | `diff A B` |
| 一次性做多步编辑 | `apply F --plan plan.json -o OUT` |
| 只做一个改动 | `op <算子> F ... -o OUT` |
| 查算子参数 | `ops --text` / `ops --describe transpose` |
| 查 JSON 结构 | `schema plan` / `schema event` / `schema selector` |
| 从零作曲 | `create OUT --note "C4 0 1 100" ...` 或 `create OUT --spec score.json` |
| 让模型直接产出整首 | 先产出 JSON 文档，再 `from-json doc.json -o OUT` |
| 把一个文件按通道拆开 | `split F.mid --out-dir stems`（每通道一个独立文件） |
| 把单轨文件炸成各声部 | `split F.mid --out-dir parts --by track` |
| 自检工具是否正常 | `selftest` |

## 2. 配方（可直接抄）

### 2.1 把某轨整体升高八度，并做力度渐变

`@bash
python midi.py op transpose F.mid --semitones 12 --match "track=1" -o /tmp/a.mid
python midi.py apply /tmp/a.mid --ops '[{"op":"set_velocity","mode":"ramp","value":50,"value_to":120,"match":{"track":1}}]' -o OUT.mid
`@

### 2.2 量化 + 摇摆 + 人性化（"让它听起来像人弹的"）

`@json
{"ops": [
  {"op": "quantize", "grid": "1/16", "strength": 0.85, "match": {"track": 1}},
  {"op": "quantize", "grid": "1/8", "swing": 0.62, "match": {"track": 2}},
  {"op": "humanize", "timing": 9, "velocity": 7, "seed": 20240607, "match": {"track": 1}}
]}
`@

### 2.3 只改副歌那一段（按小节定位）

`@bash
# 第 17-32 小节：升高 2 个半音、加响
python midi.py apply F.mid --ops '[{"op":"transpose","semitones":2,"match":{"start_min":30720,"start_max":61439}},
                                    {"op":"set_velocity","mode":"scale","value":1.1,"match":{"start_min":30720,"start_max":61439}}]' -o OUT.mid
`@

（bar N 的起始 tick = (N-1) × 每小节 tick；`1bar` 简写只在带速度映射的算子参数里可用。）

### 2.4 把 A 段循环 4 次

`@json
{"ops": [
  {"op": "copy_region", "start": "1bar", "end": "5bar", "to": "5bar", "repeat": 3, "tracks": [1, 2]}
]}
`@

### 2.5 写一段和弦进行（C - G - Am - F，每小节一个和弦）

`@bash
python midi.py create progression.mid --tempo 96 --time-signature 4/4 --unit beats --track "Pad:0:89" --note "C4 0 4 80 0 1" --note "E4 0 4 78 0 1" --note "G4 0 4 76 0 1" --note "B3 4 4 80 0 1" --note "D4 4 4 78 0 1" --note "G4 4 4 76 0 1" --note "A3 8 4 80 0 1" --note "C4 8 4 78 0 1" --note "E4 8 4 76 0 1" --note "F3 12 4 80 0 1" --note "A3 12 4 78 0 1" --note "C4 12 4 76 0 1"
`@

### 2.6 提取一段旋律为 JSON（给别的程序用）

`@bash
python midi.py to-json F.mid -o F.json --mode notes      # 音乐视图
python midi.py dump F.mid --format jsonl --limit 2000 > events.jsonl
`@

### 2.7 修复一个坏文件（缺 note_off / 重复事件 / EOT 错位）

`@bash
python midi.py validate broken.mid --text
python midi.py op repair broken.mid --dedupe --in-place --backup --text
python midi.py validate broken.mid --text      # 退出码 0 才算修好
`@

### 2.8 鼓轨清理：只保留 GM 鼓组范围内的音，去掉重复触发

`@bash
python midi.py apply F.mid --ops '[{"op":"limit_pitch_range","min_pitch":35,"max_pitch":81,"mode":"drop","match":{"channel":9}},
                                   {"op":"remove_duplicate_notes","tolerance":5,"match":{"channel":9}}]' -o OUT.mid
`@

### 2.9 变速（渐快）+ 标记段落

`@json
{"ops": [
  {"op": "set_tempo", "bpm": 100, "replace_all": true},
  {"op": "tempo_ramp", "bpm_start": 100, "bpm_end": 132, "start": "1bar", "end": "17bar", "steps": 32},
  {"op": "add_text", "meta_type": "marker", "text": "Verse", "at": "1bar"},
  {"op": "add_text", "meta_type": "marker", "text": "Chorus", "at": "17bar"}
]}
`@

### 2.10 拆分成音轨文件（交给别的软件/人继续处理）

``bash
# 每通道一个文件，各自带速度映射与段落标记，可单独播放
python midi.py split F.mid --out-dir stems --text

# 先预览文件名与音符数，不落盘
python midi.py split F.mid --out-dir stems --dry-run
``

`@json
// 返回信封节选：每份文件的通道、名称、音符数、字节数与校验结果
{"ok": true, "data": {"count": 5, "parts": [
  {"channel": 0, "channel_display": 1, "name": "Piano (Acoustic Grand Piano)",
   "notes": 165, "events": 344, "filename": "sunrise_ch01.mid", "bytes": 1883}],
  "validation": [{"ok": true}]}}
`@

要点：默认输出 format 1（Conductor 轨 + 声部轨）；加 `--no-conductor --midi-format 0` 得到单轨 format 0；
文件名模板支持 `{stem} {index} {channel} {channel1} {ch2} {name}`；拆分后每份文件都会单独做规范校验。

### 2.11 让模型"写出整首曲子"（最稳的路径）

让模型只产出下面这种 JSON，然后一条命令变成 MIDI（可用 `pianoroll` 回看、`validate` 验收）：

`@json
{"format": 1, "division": {"type": "ppq", "ticks_per_beat": 480},
 "tracks": [
   {"name": "Tempo", "events": [{"tick": 0, "type": "meta", "meta_type": "set_tempo", "bpm": 104},
                                {"tick": 0, "type": "meta", "meta_type": "time_signature", "numerator": 4, "denominator": 4}]},
   {"name": "Piano", "notes": [{"pitch": "C4", "start": 0, "duration": 480, "velocity": 96},
                               {"pitch": "E4", "start": 480, "duration": 480, "velocity": 88}]}
 ]}
`@

`@bash
python midi.py from-json song.json -o song.mid
`@

## 3. 常见错误与立刻可用的修法

| 现象（错误码） | 原因 | 修法 |
| --- | --- | --- |
| `E_SCHEMA ... unknown selector field(s)` | 选择器字段名打错 | 看 `error.details.allowed`，或 `schema selector` |
| `E_SCHEMA ... op 'x' has no parameter 'y'` | 算子参数名错 | `ops --describe x` 看合法参数 |
| `E_OP ... no notes matched selector` | 选择器没命中 | 加 `--dry-run` 前先用 `notes`/`dump` 确认范围；或去掉 `require_match` |
| `E_PARSE ... no MThd header` | 不是 MIDI 文件（可能是纯音频/文本） | 用 `file` 或 `info` 之外的方式确认；不要硬改 |
| `E_VALIDATION` | `validate ` 报 error | 看 issues 的 code（如 `V_MISSING_EOT`），用 `op repair` 或针对性算子 |
| `E_USAGE ... needs --output` | 写命令没给输出 | 加 `-o OUT` 或 `--in-place` |
| 退出码 1 且 message 提到 `not valid selector JSON` | PowerShell 吞掉了引号 | 用 `--match "track=1,pitch=C4"` 简写，或 `--match "{track:1}"`（裸键也认） |

## 4. 速度与时间换算（避免算错 tick）

- 一拍的 tick 数 = `ticks_per_beat`（PPQ，常见 480/960）
- 一小节 tick = `numerator × (4 × PPQ / denominator)`，例如 4/4、PPQ=480 → 1920
- 秒 ↔ tick 依赖速度映射：用 `notes --unit seconds` 或 `info` 的 `duration_seconds` 校验
- 在算子参数里可以直接写 `1bar` / `2b` / `500ms`，工具会按文件当前拍号与速度换算

## 5. 报告格式建议（让人类一眼看懂你改了什么）

`@json
{"ok": true, "changed": [{"op": "transpose", "notes_affected": 128, "tracks": [1]}],
 "verification": {"validate": "0 error 0 warning", "diff": {"notes_added": 0, "notes_removed": 0}},
 "output": "song_v2.mid"}
`@

## 6. 给智能体的最小系统提示（可直接复制）

`@
你可以使用 midikit 修改/生成 MIDI 文件。规则：
1) 先运行 python midi.py info/notes/pianoroll/validate 了解文件，再动手；
2) 编辑统一用 python midi.py apply F.mid --plan plan.json -o OUT.mid --dry-run 预览，成功后再去掉 --dry-run；
3) 不要覆盖原文件（除非用户要求，此时加 --in-place --backup）；
4) 命令输出是 JSON，看 data/error 字段与退出码（0 成功，1 参数错，2 校验失败，3 操作失败，4 解析失败）；
5) 遇到 E_SCHEMA 时按 error.details.allowed / python midi.py ops --describe <算子> 修正参数；
6) 完成后运行 validate 与 diff，并汇报：改了哪些轨道、影响多少音符、输出文件路径。
`@
