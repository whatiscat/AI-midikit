> [!IMPORTANT]
> ### 🤖 本项目由 AI 生成 · AI-Generated Project
>
> 本仓库的**全部代码**（`midikit/` 库、39 个编辑算子、命令行工具、134 项测试、文档与示例）均由
> **AI 编码智能体**编写；人类负责提出需求、给出反馈与验收。
> **在用于生产环境前，请自行审查、测试并评估。**
>
> **English** — Every line of code, test and document in this repository was written by an
> **AI coding agent**; a human supplied the requirements and reviewed the outcome.
> Please review and test it yourself before using it in production.

# midikit — 面向 AI 智能体的标准 MIDI 文件工具包

[![tests](https://github.com/whatiscat/AI-midikit/actions/workflows/tests.yml/badge.svg)](https://github.com/whatiscat/AI-midikit/actions/workflows/tests.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)

`midikit` 让智能体像读写 JSON 一样，**精准、可验证、可回滚**地读取 / 修改 / 生成标准 MIDI 文件（Standard MIDI Files 1.1）。

- **零依赖**：纯 Python 标准库（3.8+），不需要 mido / pretty_midi / numpy
- **全规范覆盖**：format 0/1/2、PPQ 与 SMPTE 时基、running status、全部通道消息、全部 meta 事件（含未知类型原样保留）、F0 sysex 与 F7 escape、未知块（XFIH/XFKM…）原样保留、RIFF/RMID 自动解包
- **JSON 进 JSON 出**：每条命令都输出统一信封，机器可解析，错误带稳定错误码
- **精确寻址**：轨道索引 + 事件索引 + 声明式选择器（类型/通道/tick/音高/力度/文本）
- **安全编辑**：先全部算完再落盘，原子写、`--dry-run`、`--in-place --backup`
- **39 个算子**：从"插入一个 CC"到"移调 + 量化 + 摇摆 + 速度渐变"
- **能被"看懂"**：文本钢琴卷帘、语义 diff、结构统计、规范校验器

---

## 1. 快速开始

`@bash
# 概览
python midi.py info song.mid
python midi.py info song.mid --text          # 人类友好输出

# 看音乐（文本钢琴卷帘）
python midi.py pianoroll song.mid --text --height 20 --width 100

# 列出音符（带小节/拍/秒/拍数换算）
python midi.py notes song.mid --unit beats

# 校验 SMF 1.1 合规性（有 error 时退出码 2）
python midi.py validate song.mid --text

# 单个算子：把 1 轨升高 12 个半音
python midi.py op transpose song.mid --semitones 12 --match "track=1" -o up.mid

# 多个算子：一次编辑计划
python midi.py apply song.mid --plan examples/edit_plan.json -o edited.mid

# 从零生成一个文件
python midi.py create out.mid --tempo 120 --time-signature 4/4 --unit beats --track "Piano:0:0" --note "C4 0 1 100 0 1" --note "E4 1 1 90 0 1" --note "G4 2 2 85 0 1"
`@

所有命令默认输出 JSON（`--text` 切换为人类可读）。默认不覆盖输入文件：必须显式给 `-o` 或 `--in-place`。

仓库自带可直接运行的示例（无需准备素材）：

`@bash
python midi.py info examples/demo.mid --text
python midi.py pianoroll examples/demo.mid --text
python midi.py apply --plan examples/demo_plan.json --text   # 8 个算子的完整演示
python midi.py from-json examples/score.json -o my_song.mid  # 从乐谱 JSON 生成
`@

想装成命令（可选，零依赖）：

`@bash
pip install -e .
midikit info examples/demo.mid
`@

---

## 2. 命令一览

| 命令 | 作用 | 是否写文件 |
| --- | --- | --- |
| `info` | 头信息、轨道、速度、音符统计、时长（tick/秒/小节） | 否 |
| `tracks` | 轨道列表 | 否 |
| `dump` | 事件级导出，带 `track`/`index` 寻址、小节/拍/秒 | 否 |
| `notes` | 音符列表（配对好的 note_on/note_off） | 否 |
| `stats` | 音高直方图、力度统计、复音数、密度 | 否 |
| `pianoroll` | 文本钢琴卷帘（可直接给模型"看"） | 否 |
| `validate` | SMF 1.1 合规检查（error/warning/info） | 否 |
| `diff` | 两个文件的语义差异（事件/音符/头部） | 否 |
| `split` | 按通道/按轨拆成多个独立文件（每个都自带速度与标记） | 是（多文件） |
| `to-json` | 整文件导出为 JSON 文档 | 可选 |
| `from-json` | 由 JSON 文档生成 MIDI | 是 |
| `create` | 生成新文件（命令行音符 / 乐谱 JSON / 计划） | 是 |
| `apply` | 执行 JSON 编辑计划 | 是 |
| `op <名称>` | 执行单个算子（每个算子有独立 `--help`） | 是 |
| `ops` | 列出/详述所有算子 | 否 |
| `schema` | 输出 plan/document/event/note/selector 等 JSON 结构 | 否 |
| `selftest` | 内置自检（12 项不变量） | 否 |
| `version` | 版本 | 否 |

常用参数（读写命令都支持）：

`@bash
-o, --output PATH        输出路径
--in-place               覆盖输入
--backup                 覆盖前备份为 xxx.mid.bak.mid
--dry-run                只报告不写盘
--format json|text|jsonl 输出格式（默认 json）
--compact                单行 JSON
--strict                 严格解析 / 更严格的校验
--encoding auto|utf-8|latin-1    meta 文本编码
--running-status         写文件时使用 running status 压缩
--note-off-as-note-on-0  note_off 写成 velocity=0 的 note_on
`@

---

## 3. 编辑计划（plan）

计划是一个 JSON：`{"ops": [ ... ]}`，按顺序执行；**任一算子失败则整个计划不落盘**（内存里改，全部成功才写）。

`@json
{
  "input": "song.mid",
  "output": "song_v2.mid",
  "options": {"backup": true, "running_status": false},
  "ops": [
    {"op": "quantize", "grid": "1/16", "strength": 0.9, "match": {"track": 1}},
    {"op": "humanize", "timing": 8, "velocity": 6, "seed": 7, "match": {"track": 1}},
    {"op": "transpose", "degrees": 3, "scale": "major", "root": "C", "match": {"track": 2}},
    {"op": "set_velocity", "mode": "ramp", "value": 40, "value_to": 110, "match": {"track": 2}},
    {"op": "tempo_ramp", "bpm_start": 96, "bpm_end": 128, "start": "8bar", "end": "16bar", "steps": 24},
    {"op": "add_text", "meta_type": "marker", "text": "Chorus", "at": "16bar"},
    {"op": "copy_region", "start": "16bar", "end": "24bar", "to": "24bar", "repeat": 1, "tracks": [1, 2]},
    {"op": "repair", "dedupe": true}
  ]
}
`@

命令行执行：

`@bash
python midi.py apply --plan plan.json                 # input/output 写在 plan 里
python midi.py apply song.mid --plan plan.json -o out.mid
python midi.py apply song.mid --ops '[{"op":"transpose","semitones":2}]' -o out.mid
`@

时间参数（`at`/`start`/`end`/`delta`/`duration` 等）可以是：

| 写法 | 含义 |
| --- | --- |
| `480` | 480 tick（默认单位就是 tick） |
| `2b` / `2beats` | 2 拍 |
| `1bar` | 1 小节（按当前拍号） |
| `250ms` / `1.5s` | 毫秒 / 秒（按当前速度映射） |
| `3:2:120` | 第 3 小节第 2 拍第 120 tick |

网格参数 `grid` 可以是 `1/16`、`eighth`、`1/8t`（三连）、`1/4.`（附点）、`beat`、`bar` 或 tick 数。

---

## 4. 选择器（selector）

选择器用来**精确指定要编辑哪些事件/音符**；字段全部可选，`{}` 表示全部。

事件选择器（`delete_events`/`update_events`/`move_events`/`copy_events`）：

`@json
{"track": 1, "type": "note_on", "channel": 0,
 "tick_min": 0, "tick_max": 7680,
 "note_min": 60, "note_max": 72,
 "velocity": 100, "velocity_min": 1, "velocity_max": 63,
 "control": "pan", "program": "Cello", "value_min": 64,
 "meta_type": "lyric", "text_contains": "la",
 "index": 12, "limit": 8, "which": "first"}
`@

其中 `control` 可写编号或名称（`pan`/`sustain`/`reverb_send`…），`program` 可写编号或 GM 名称；
`value` / `value_min` / `value_max` 作用于 control_change（CC 值）、pitch_bend（0-16383）、program_change、aftertouch，
音符力度请用 `velocity` / `velocity_min` / `velocity_max`。

音符选择器（`delete_notes`/`transpose`/`quantize`/`set_velocity`…）：

`@json
{"track": 2, "channel": [0, 1], "note_min": 36, "note_max": 96,
 "start_min": 1920, "duration_max": 240, "velocity_min": 20, "limit": 100}
`@

命令行简写：`--match '{"track":1}'`、`--match "track=1,pitch=C4"`、`--match note_on` 三种写法都接受。

---

## 5. 事件与音符 JSON

**事件**（`dump` 输出 / `insert_events` 输入）：

`@json
{"tick": 0, "type": "note_on", "channel": 0, "note": 60, "velocity": 100}
{"tick": 0, "type": "control_change", "channel": 0, "control": 7, "value": 100}
{"tick": 0, "type": "program_change", "channel": 1, "program": 42}
{"tick": 0, "type": "pitch_bend", "channel": 0, "semitones": -0.5}
{"tick": 0, "type": "meta", "meta_type": "set_tempo", "bpm": 128}
{"tick": 0, "type": "meta", "meta_type": "time_signature", "numerator": 6, "denominator": 8}
{"tick": 0, "type": "meta", "meta_type": "key_signature", "key": "F#m"}
{"tick": 0, "type": "meta", "meta_type": "lyric", "text": "Hello"}
{"tick": 0, "type": "sysex", "data_hex": "f0 41 10 42 12 40 00 7f 00 41 f7"}
`@

type 支持：`note_on` `note_off` `poly_aftertouch` `control_change` `program_change` `channel_aftertouch` `pitch_bend` `meta` `sysex` `escape` `system`；
别名也认：`note` `cc` `program` `aftertouch` `pitchwheel` `polytouch`。

**音符**（`notes` 输出 / `insert_notes` 输入）：

`@json
{"pitch": "C4", "start": 0, "duration": 480, "velocity": 100, "channel": 0, "track": 1}
`@

也接受字符串写法 `'C4 0 480 100 0 1'` = 音高 起点 时值 [力度] [通道] [轨道]。
时值单位由 `unit` 决定：`ticks`（默认）/ `beats` / `seconds` / `bars`。

---

## 6. 算子清单（39 个）

**音符类**

| 算子 | 说明 |
| --- | --- |
| `insert_notes` | 写入音符（自动配对 note_on/note_off） |
| `delete_notes` | 按选择器删除音符 |
| `update_notes` | 改音高/力度/起点/时值/通道（支持相对偏移） |
| `transpose` | 半音移调或调式内级数移调（`scale`+`root`+`degrees`） |
| `quantize` | 量化到网格，支持强度与 swing |
| `humanize` | 时值/力度抖动（seed 可复现） |
| `set_velocity` | set / scale / offset / limit / ramp / random |
| `set_note_length` | set / scale / gate / legato / staccato |
| `limit_pitch_range` | 折回八度 / 夹紧 / 丢弃 |
| `remove_duplicate_notes` | 去重（保留首个/最长/最响） |
| `strum` | 和弦扫弦错开 |
| `crop` | 裁剪时间区间（越界音符自动剪短） |

**事件类**

| 算子 | 说明 |
| --- | --- |
| `insert_events` | 插入任意事件（CC/弯音/meta/sysex…） |
| `delete_events` | 按选择器删除事件 |
| `update_events` | 批量改字段 |
| `move_events` | 平移/按秒平移/对齐/缩放时间 |
| `copy_events` | 复制事件到目标轨/位置（可重复 N 次） |
| `copy_region` | 复制一个时间区间的全部内容（乐句循环） |
| `dedupe_events` | 删除完全重复事件 |
| `sort_events` | 稳定排序（note_off 先于 note_on） |
| `set_channel` | 改通道 |
| `set_instrument` | 写 program_change（可含 bank select / 音色名） |

**结构类**

| 算子 | 说明 |
| --- | --- |
| `insert_track` `delete_track` `move_track` `clone_track` | 轨道增删移动复制 |
| `merge_tracks` `split_track_by_channel` | 合并 / 按通道拆分 |
| `set_track_name` | 重命名（写 track_name meta） |
| `set_format` | 0/1/2 格式转换（转 0 自动合并） |
| `set_division` | 改 PPQ 或 SMPTE，可按时间等价重算所有 tick |
| `repair` | 排序/去重/补 note_off/删孤儿 note_off/规范 EOT |

**速度与 meta**

| 算子 | 说明 |
| --- | --- |
| `set_tempo` | 设置速度（BPM 或微秒），可替换全部速度 |
| `scale_tempo` | 整体缩放速度 / 统一固定 BPM |
| `tempo_ramp` | 线性或指数速度渐变 |
| `set_time_signature` `set_key_signature` | 拍号、调号 |
| `add_text` | 歌词/标记/版权/音轨名等文本 meta |
| `strip_meta` | 清理 meta（可保留速度/拍号/调号/EOT） |

`python midi.py ops --text` 查看全部；`python midi.py ops --describe transpose` 查看单个算子的 JSON 结构；`python midi.py op transpose --help` 查看该算子的命令行参数。

---

## 7. Python API

`@python
from midikit import MidiFile, apply_plan, extract_notes, TempoMap, validate

midi = MidiFile.read("song.mid")          # strict=True 严格解析
print(midi.format, midi.division.describe(), len(midi.tracks))

tm = TempoMap.from_midi(midi)
notes = extract_notes(midi).notes         # 已配对好的音符
print(notes[0].pitch, notes[0].start, tm.tick_to_seconds(notes[0].start))

result = apply_plan(midi, [
    {"op": "quantize", "grid": "1/16", "match": {"track": 1}},
    {"op": "transpose", "semitones": 2,  "match": {"track": 1}},
])
assert result["ok"], result["failed"]
midi.write("song_edited.mid")             # 原子写；自动补齐 end_of_track

from midikit import from_document, to_document
doc = to_document(midi, mode="events")    # 无损
other = from_document(doc)
assert other.to_bytes() == midi.to_bytes()
`@

自定义算子只需一个装饰器：

`@python
from midikit.ops import register, Param, OpContext, apply_note_transform

@register("double_velocity", "力度翻倍", category="note",
          params=[Param("match", "selector", default={})])
def op_double_velocity(ctx: OpContext, p):
    result = apply_note_transform(
        ctx, p["match"],
        lambda n: setattr(n, "velocity", min(127, n.velocity * 2)) or n,
    )
    return {"updated": result["notes_affected"]}
`@

---

## 8. 规范覆盖（SMF 1.1）

| 规范条目 | 支持情况 |
| --- | --- |
| MThd 头块、长度 ≥ 6、多余字节保留 | 支持 |
| format 0 / 1 / 2 | 读写 + 转换 |
| ntrks 与实际块数不一致 | 告警并可修复 |
| 时基：PPQ（1-32767）与 SMPTE（-24/-25/-29/-30 × ticks/frame） | 读写、互转 |
| MTrk 块、delta time（VLQ，1-4 字节） | 含超长 VLQ 容错 |
| Running status（读 + 可选写） | 遇 meta/sysex 正确复位 |
| 通道消息 0x8n-0xEn 全部 7 类 | 含 note-on velocity=0 视作 note-off |
| Meta 事件 0x00-0x7F（文本/EOT/速度/拍号/调号/SMPTE offset/通道前缀/端口/音序器专用） | 语义解析 |
| 未知 meta 类型 | 原样保留（读写字节一致） |
| F0 sysex / F7 escape（含续传） | 支持 |
| EOT 缺失 / 位置错误 / 重复 | 检出并在写盘时规范为"最后一个事件" |
| 轨道内出现 F1-FE 非规范系统消息 | 解析为 system 事件并在 `validate` 中报 error |
| 未知块（XFIH/XFKM 等） | 原样保留并在写回时输出 |
| RIFF/RMID 包装文件 | 自动解包 |
| 文本编码（ASCII/UTF-8/CP1252/Latin-1） | 自动探测，不破坏原始字节 |
| tick 超过 0x0FFFFFFF、数据字节越界、负 delta | 检出报错 |

`validate` 输出分级：`error`（违反规范）/ `warning`（可疑但不违规）/ `info`（提示）。有 error 时退出码 2，`--strict` 下 warning 也算失败。

---

## 9. 退出码与错误信封

| 退出码 | 含义 | 错误码 |
| --- | --- | --- |
| 0 | 成功 | — |
| 1 | 参数/结构错误 | `E_USAGE` `E_SCHEMA` |
| 2 | 校验失败 | `E_VALIDATION` |
| 3 | 操作失败（如选择器没有命中） | `E_OP` `E_NOT_FOUND` |
| 4 | 解析失败（不是合法 MIDI） | `E_PARSE` |
| 5 | 写文件失败 | `E_WRITE` |
| 70 | 内部错误 | `E_INTERNAL` |

成功：

`@json
{"ok": true, "command": "apply", "file": "in.mid", "output_path": "out.mid",
 "data": {"ops_applied": 3, "validation": {"ok": true, "counts": {"error": 0}}}}
`@

失败（错误里带 `allowed`/`available` 等提示，智能体可据此自我纠正）：

`@json
{"ok": false, "command": "op delete_notes",
 "error": {"code": "E_SCHEMA",
           "message": "unknown note selector field(s): pitchh",
           "details": {"allowed": ["track", "channel", "pitch", "..."]}}}
`@

---

## 10. 智能体推荐工作流

1. **先看**：`info` → `notes` / `pianoroll`（建立音乐直觉）→ `validate`（确认文件健康）
2. **再定位**：用 `dump --type note_on --tick-min ... --tick-max ...` 或 `ops --describe` 拿到精确坐标
3. **小步改**：`apply --dry-run` 看每个算子的报告（命中数量、影响轨道），确认后再落盘
4. **写完验**：`validate` + `diff` 对比修改前后
5. **可回滚**：`--in-place --backup`，或始终写新文件

`@bash
# 典型闭环
python midi.py validate in.mid --text
python midi.py apply in.mid --plan edits.json -o out.mid --dry-run
python midi.py apply in.mid --plan edits.json -o out.mid
python midi.py validate out.mid --text
python midi.py diff in.mid out.mid
`@

更完整的配方见 [AGENT_GUIDE.md](AGENT_GUIDE.md)。

---

## 11. 目录结构

`@
midi.py                      CLI 启动器（任意目录可用）
midikit/
  bytestream.py              VLQ、大端整数、块、hex 工具
  constants.py               meta/CC/GM 音色/鼓组/调号/音阶 表
  events.py                  事件模型 + JSON 互转
  smf.py                     MidiFile / MidiTrack / Division（读写核心）
  tempo.py                   TempoMap：tick ↔ 秒 ↔ 小节:拍
  notes.py                   音符配对提取
  selectors.py               事件/音符选择器
  validate.py                规范校验
  analysis.py                统计 / 钢琴卷帘 / 语义 diff
  jsonio.py                  整文件 JSON 文档互转
  split.py                   按通道/按轨拆分独立文件
  plan.py                    编辑计划执行器
  ops/                       39 个算子（registry + event/note/structure）
  cli.py                     命令行
tests/test_midikit.py        98 个功能单元测试（含 mido 交叉验证）
tests/test_verification.py   36 个硬化测试（全算子矩阵 / 可逆性 / 模糊 / CLI 矩阵 / 拆分）
tools/realworld_check.py     真实 MIDI 语料批量体检（含与 mido 的逐音符比对）
examples/                    示例计划 / 乐谱 / 示例文件
`@

---

## 12. 测试与自检

`@bash
python tests/test_midikit.py                    # 98 个功能测试
python tests/test_verification.py               # 36 个硬化测试
python midi.py selftest --text                  # 12 项内置不变量
python tools/realworld_check.py "$HOME/Music"   # 真实语料批量体检
`@

`realworld_check` 对每个文件做：解析 → 重新序列化 → 再解析（要求事件流一致、字节幂等）→ 规范校验 → 移调 +3/-3 往返（要求音符完全复原）→ 与 mido 交叉比对格式/轨道数/时基，并**逐音符比对**（轨道/通道/音高/起止/力度全部一致）

`test_verification` 覆盖：39 个算子逐个实跑（并要求不破坏文件、不产生悬空/孤儿音符、JSON 往返仍字节一致）、算子可逆性、随机文件模糊测试（含 running status 与 velocity=0 写法）、截断/位翻转输入不崩溃、全部 CLI 命令的退出码与 JSON 信封、拆分功能的通道/轨道/格式/命名/冲突处理。

---

## 13. 关于本项目（AI 生成说明）

> **本项目由 AI 生成。** 全部代码、测试与文档均由 AI 编码智能体编写；人类负责提出需求、反馈与验收。
> 请在用于生产环境前自行审查与测试。

- **生成方式**：由 AI 编码智能体在人类需求驱动下完成设计、编码、测试与文档；每一步都用自动化验证兜底
- **人类参与**：提出需求与方向、验收结果、决定发布（GitHub 账号、仓库设置与 Release 均为人操作）
- **可验证性**：`tests/` 下 134 项自动化测试、`python midi.py selftest` 12 项内置不变量、
  `tools/realworld_check.py` 真实语料体检（含与 mido 的**逐音符比对**）、
  GitHub Actions 在 Windows/Linux × Python 3.8/3.13 上全部通过 —— 不必只凭信任，跑一遍即可
- **免责**：AI 生成的代码可能包含错误、或存在与你预期不同的设计取舍；本项目按 MIT 许可"按原样"提供，不附带任何担保
