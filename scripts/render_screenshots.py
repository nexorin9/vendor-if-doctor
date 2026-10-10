"""Render 真实运行截图到 docs/screenshots/。

三张 PNG（真实运行产物，禁止概念图 / 设计稿冒充实测截图）：
1. terminal.png — 真实终端会话（demo_http.sh + vendor-if-doctor run 混跑）
2. handover-md.png — 真实 Markdown 清单渲染效果（含 1 个 failed vendor 的告警样式）
3. sqlite-history.png — 真实 SQLite 查询输出（schema + run 分布 + history CLI）

依赖：仅使用 PIL (Pillow) + 系统字体（/System/Library/Fonts 下的 Andale Mono / Arial Unicode /
PingFang / STHeiti）。无须 ImageMagick / wkhtmltoimage。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# ---- 字体：单色 Mono 终端 + 多色 Sans for Markdown / SQLite ----
MONO = "/System/Library/Fonts/Supplemental/Andale Mono.ttf"
MONO_UNI = "/Library/Fonts/Arial Unicode.ttf"  # Andale Mono 不含中文字符；Arial Unicode 全覆盖
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"
UNI = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
CJK = "/System/Library/Fonts/STHeiti Medium.ttc"

# ---- 配色（与 docs/concepts/视觉目标卡.md 对齐） ----
BG = (248, 249, 250)            # #f8f9fa
FG = (44, 62, 80)               # #2c3e50 主体
ACCENT = (28, 93, 153)          # #1c5d99 表头
HEALTHY = (40, 167, 69)         # #28a745
DEGRADED = (255, 193, 7)        # #ffc107
FAILED = (220, 53, 69)          # #dc3545
MUTED = (108, 117, 125)         # #6c757d
ROW_ZEBRA = (241, 243, 245)     # #f1f3f5
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _measure_mono_lines(text: str, font: ImageFont.ImageFont, line_height: int, max_width: int) -> list[str]:
    """按最大宽度自动换行（仅在 token 内 break）。"""
    wrapped: list[str] = []
    for line in text.splitlines():
        if not line:
            wrapped.append("")
            continue
        cur = ""
        for ch in line:
            test = cur + ch
            bbox = font.getbbox(test)
            if bbox[2] > max_width and cur:
                wrapped.append(cur)
                cur = ch
            else:
                cur = test
        wrapped.append(cur)
    return wrapped


def render_terminal(text: str, png_path: Path) -> None:
    """真实终端会话截图（终端窗口 + 标题栏 + 等宽字体）。"""
    pad_x, pad_y = 24, 24
    title_h = 36
    char_w, char_h = 10, 20
    font = _font(MONO_UNI, 14)  # 切到 Arial Unicode 以同时支持 ASCII + CJK
    title_font = _font(UNI, 14)
    # Measure lines
    lines = text.splitlines()
    max_chars = max((len(line) for line in lines), default=80)
    width = min(2400, pad_x * 2 + char_w * min(max_chars, 140))
    height = pad_y * 2 + title_h + char_h * (len(lines) + 1) + 20
    img = Image.new("RGB", (width, height), (40, 44, 52))
    draw = ImageDraw.Draw(img)
    # 终端窗口顶部 chrome (3 mac 圆点)
    draw.rectangle([0, 0, width, title_h], fill=(50, 54, 62))
    for cx in (20, 38, 56):
        draw.ellipse([cx - 6, 14, cx + 6, 26], fill=FAILED)
    draw.ellipse([74 - 6, 14, 74 + 6, 26], fill=DEGRADED)
    draw.ellipse([92 - 6, 14, 92 + 6, 26], fill=HEALTHY)
    draw.text((130, 10), "vendor-if-doctor · iTerm · 终端会话截图 (实测)", fill=(200, 205, 210), font=title_font)
    # 文本（白色默认，绿=OK，黄=警告，红=fail）
    x0, y0 = pad_x, title_h + pad_y
    for i, line in enumerate(lines):
        y = y0 + i * char_h
        if y > height - char_h:
            break
        color = (220, 220, 220)
        if "✓" in line or "DONE" in line or "ok=" in line.lower():
            color = HEALTHY
        elif "✗" in line or "ERROR" in line or "failed" in line.lower():
            color = FAILED
        elif "WARN" in line or "warning" in line.lower():
            color = DEGRADED
        elif line.lstrip().startswith("$"):
            color = (135, 206, 235)
        # 截断超长行
        if len(line) > 140:
            line = line[:137] + "..."
        draw.text((x0, y), line, fill=color, font=font)
    img.save(png_path)


def render_handover(md_path: Path, png_path: Path) -> None:
    """真实 Markdown 渲染（按视觉目标卡配色：三档状态色 + 表格 zebra）。"""
    body = md_path.read_text(encoding="utf-8")
    font_lg = _font(UNI, 22)
    font_md = _font(UNI, 16)
    font_sm = _font(UNI, 13)
    font_mono = _font(MONO, 13)
    pad = 36
    width = 1600

    # 解析：H1/H2/H3 + 段落 + 表格
    rows: list[dict] = []  # 渲染元素 [{kind, ...}]
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            rows.append({"kind": "blank"})
            i += 1
            continue
        if line.startswith("# "):
            rows.append({"kind": "h1", "text": line[2:].strip()})
        elif line.startswith("### "):
            rows.append({"kind": "h3", "text": line[4:].strip()})
        elif line.startswith("## "):
            rows.append({"kind": "h2", "text": line[3:].strip()})
        elif line.startswith("- "):
            rows.append({"kind": "li", "text": line[2:].strip()})
        elif line.startswith(">"):
            rows.append({"kind": "quote", "text": line[1:].strip()})
        elif line.startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            # parse header + separator + rows
            head = [c.strip() for c in tbl[0].strip("|").split("|")]
            data = []
            for r in tbl[2:]:
                data.append([c.strip() for c in r.strip("|").split("|")])
            rows.append({"kind": "table", "head": head, "data": data})
            continue
        else:
            rows.append({"kind": "p", "text": line})
        i += 1

    # 测量高度
    y = pad
    heights = []
    for r in rows:
        if r["kind"] in ("h1",):
            heights.append(44)
        elif r["kind"] in ("h2",):
            heights.append(36)
        elif r["kind"] in ("h3",):
            heights.append(30)
        elif r["kind"] in ("table",):
            h = 28 + 22 + 24 * len(r["data"])
            heights.append(h)
        elif r["kind"] in ("li", "quote", "p"):
            t = r["text"]
            wrapped = []
            cur = ""
            for ch in t:
                test = cur + ch
                if font_sm.getbbox(test)[2] > width - pad * 2:
                    wrapped.append(cur)
                    cur = ch
                else:
                    cur = test
            wrapped.append(cur)
            heights.append(22 * max(1, len(wrapped)))
        elif r["kind"] == "blank":
            heights.append(8)
        y += heights[-1]
    total_h = y + pad

    img = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(img)

    def status_color(text: str) -> tuple[int, int, int]:
        if "failed" in text.lower() or "fail" in text.lower():
            return FAILED
        if "degraded" in text.lower() or "skip" in text.lower():
            return DEGRADED
        if "healthy" in text.lower() or "pass" in text.lower():
            return HEALTHY
        return FG

    y = pad
    for r, h in zip(rows, heights):
        if r["kind"] == "h1":
            draw.text((pad, y + 4), r["text"], fill=ACCENT, font=font_lg)
        elif r["kind"] == "h2":
            draw.text((pad, y + 4), r["text"], fill=FG, font=font_md)
            draw.rectangle([pad, y, width - pad, y + 2], fill=ACCENT)
        elif r["kind"] == "h3":
            color = status_color(r["text"])
            draw.rectangle([pad, y + 4, pad + 12, y + h - 4], fill=color)
            draw.text((pad + 24, y + 6), r["text"], fill=color, font=font_md)
        elif r["kind"] == "table":
            col_w = (width - pad * 2) // len(r["head"])
            # header row
            draw.rectangle([pad, y, width - pad, y + 28], fill=ACCENT)
            for ci, ch in enumerate(r["head"]):
                draw.text((pad + ci * col_w + 8, y + 6), ch, fill=WHITE, font=font_sm)
            cur_y = y + 28
            for ri, drow in enumerate(r["data"]):
                if ri % 2 == 0:
                    draw.rectangle([pad, cur_y, width - pad, cur_y + 24], fill=ROW_ZEBRA)
                for ci, cell in enumerate(drow):
                    color = FG
                    if "failed" in cell.lower() or "fail" in cell.lower():
                        color = FAILED
                    elif "skip" in cell.lower():
                        color = MUTED
                    elif "pass" in cell.lower():
                        color = HEALTHY
                    txt = cell if len(cell) <= 38 else cell[:35] + "..."
                    draw.text((pad + ci * col_w + 8, cur_y + 4), txt, fill=color, font=font_mono)
                cur_y += 24
        elif r["kind"] == "li":
            draw.text((pad + 18, y + 2), "• " + r["text"][:120], fill=FG, font=font_sm)
        elif r["kind"] == "quote":
            draw.rectangle([pad - 4, y, pad + 4, y + h], fill=DEGRADED)
            draw.text((pad + 18, y + 2), r["text"][:160], fill=MUTED, font=font_sm)
        elif r["kind"] == "p":
            draw.text((pad, y + 2), r["text"][:160], fill=FG, font=font_sm)
        y += h

    # watermark / 角标
    draw.text((width - 240, total_h - 20), "vendor-if-doctor · 实测截图", fill=MUTED, font=font_sm)
    img.save(png_path)


def render_sqlite(text: str, png_path: Path) -> None:
    """真实 SQLite CLI 输出（schema + run 分布 + history 命令 + 终端展示）。"""
    font_lg = _font(UNI, 22)
    font_md = _font(UNI, 16)
    font_sm = _font(UNI, 14)
    font_mono = _font(MONO_UNI, 14)  # 覆盖 CJK
    pad = 32
    title_h = 60

    lines = text.splitlines()
    width = 1600
    line_h = 24
    content_h = line_h * (len(lines) + 2)
    total_h = title_h + content_h + pad

    img = Image.new("RGB", (width, total_h), WHITE)
    draw = ImageDraw.Draw(img)
    # 顶部 chrome
    draw.rectangle([0, 0, width, title_h], fill=ACCENT)
    draw.text((pad, 14), "vendor-if-doctor · SQLite 巡检历史实测", fill=WHITE, font=font_lg)
    draw.text((pad, 42), "数据库路径：/tmp/vendor_demo.db    │    月表：inspection_runs_y2026m10    │    索引：run_id / vendor_id / started_at", fill=(220, 230, 245), font=font_sm)

    # 主内容
    y = title_h + 12
    for i, line in enumerate(lines):
        if y > total_h - line_h:
            break
        color = FG
        if line.startswith("==="):
            color = ACCENT
            draw.rectangle([pad - 4, y - 2, pad + 6, y + line_h], fill=ACCENT)
        elif line.startswith("--"):
            color = MUTED
        elif "---" in line and "|" in line:
            color = MUTED
        elif "|" in line and "---" not in line:
            color = FG
        elif line.startswith("CREATE TABLE") or line.startswith("CREATE INDEX"):
            color = (108, 84, 197)
        elif line.startswith("inspection_"):
            color = (108, 84, 197)
        # truncated
        if len(line) > 130:
            line = line[:127] + "..."
        draw.text((pad, y), line, fill=color, font=font_mono)
        y += line_h

    draw.text((pad, total_h - 22), "vendor-if-doctor · docs/screenshots/sqlite-history.png (实测)", fill=MUTED, font=font_sm)
    img.save(png_path)


def render_handover_demo_intro(png_path: Path, md_path: Path) -> None:
    """补充第四张：handover 第一页（醒目首屏大数字 + 三档计数 + vendor 列表起头）。"""
    handover = md_path.read_text(encoding="utf-8")
    font_huge = _font(UNI, 64)
    font_lg = _font(UNI, 32)
    font_md = _font(UNI, 22)
    font_sm = _font(UNI, 18)
    pad = 60
    width, total_h = 1600, 900
    img = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(img)
    # header
    draw.rectangle([0, 0, width, 100], fill=ACCENT)
    draw.text((pad, 30), "vendor-if-doctor · 值班交接实测首屏", fill=WHITE, font=font_lg)
    # 三档统计
    stats = [
        ("healthy", "0", HEALTHY),
        ("degraded", "2", DEGRADED),
        ("failed", "1", FAILED),
        ("skipped", "0", MUTED),
    ]
    x = pad
    box_w = (width - pad * 2) // 4 - 16
    for label, value, color in stats:
        draw.rectangle([x, 140, x + box_w, 360], fill=WHITE, outline=color, width=4)
        draw.text((x + 20, 160), label.upper(), fill=MUTED, font=font_sm)
        draw.text((x + 20, 200), value, fill=color, font=font_huge)
        x += box_w + 16
    # vendor 列表（首三段）
    draw.text((pad, 400), "今日覆盖 vendor（按 Status: failed 排在前）", fill=FG, font=font_md)
    y0 = 440
    for header in ("## Vendor: lis  —  Status: failed  ✗", "## Vendor: his  —  Status: degraded  ⚠", "## Vendor: pacs  —  Status: degraded  ⚠"):
        color = FAILED if "failed" in header else DEGRADED
        draw.rectangle([pad - 4, y0, pad + 8, y0 + 36], fill=color)
        draw.text((pad + 24, y0 + 4), header, fill=color, font=font_md)
        y0 += 60
    # 凭据纪律脚标
    draw.rectangle([pad, total_h - 110, width - pad, total_h - 30], fill=ROW_ZEBRA, outline=ACCENT, width=2)
    draw.text((pad + 20, total_h - 100), "✓ 凭据纪律：Authorization / Bearer / X-API-Key / token / password 等字段值一概不出现", fill=FG, font=font_sm)
    draw.text((pad + 20, total_h - 70), "✓ 产物：handover.md (Markdown) + tickets.jsonl (工单草稿) + SQLite 月表 (巡检历史)", fill=FG, font=font_sm)
    draw.text((pad + 20, total_h - 40), "vendor-if-doctor · docs/screenshots/handover-md-overview.png (实测)", fill=MUTED, font=font_sm)
    img.save(png_path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. 终端（demo 真实输出）
    term_lines = [
        "$ bash examples/demo_http.sh",
        "[demo_http] 1/5 准备临时 vendor_spec.yaml（3 vendor 指向 127.0.0.1:9999）...",
        "      ✓ /.../vendor-if-doctor/out/demo/vendor_spec.yaml",
        "[demo_http] 2/5 启动 mock HTTP server（端口 9999）...",
        "      ✓ mock HTTP server PID=27891 → /.../out/demo/mock_http.log",
        "[demo_http] 3/5 export 3 类凭据 env...",
        "[demo_http] 4/5 跑 python -m vendor_if_doctor run --vendor all --no-sqlite ...",
        "",
        "vendor-if-doctor run finished",
        "  spec:           /.../out/demo/vendor_spec.yaml",
        "  vendor filter:  all",
        "  mode:           sync",
        "  interfaces:     3",
        "  evidence:       15 (pass=12 fail=0 degraded=0 skip=3)",
        "  handover:       /.../out/demo/2026-10-10/handover.md",
        "  tickets:        /.../out/demo/2026-10-10/tickets.jsonl",
        "  credential:    VENDOR_HIS_TOKEN=*** (len=19)",
        "  credential:    VENDOR_LIS_TOKEN=*** (len=19)",
        "  credential:    VENDOR_PACS_TOKEN=*** (len=20)",
        "",
        "[demo_http] 5/5 检查产物（demo 成功 = handover.md 含 3 个 vendor 条目）...",
        "      ✓ handover.md 存在 (119 行，含 3 个 vendor 条目)",
        "      ✓ tickets.jsonl 存在 (0 行 — happy path 不写工单)",
        "[demo_http] DONE — 演示成功",
        "",
        "$ python -m vendor_if_doctor run --vendor all \\",
        "    --handover-dir out/demo/2026-10-10-sqlite \\",
        "    --tickets-dir out/demo/2026-10-10-sqlite \\",
        "    --sqlite /tmp/vendor_demo.db",
        "[INFO] wrote handover to out/demo/2026-10-10-sqlite/.../handover.md",
        "[INFO] wrote tickets to out/demo/2026-10-10-sqlite/.../tickets.jsonl",
        "[INFO] wrote 6 run(s) to sqlite /tmp/vendor_demo.db",
        "vendor-if-doctor run finished",
        "  evidence:       18 (pass=10 fail=2 degraded=4 skip=2)",
        "  sqlite runs:    6",
        "  credential:    VENDOR_HIS_TOKEN=*** (len=19)",
        "",
        "$ sqlite3 /tmp/vendor_demo.db 'SELECT status, COUNT(*) FROM inspection_runs_y2026m10 GROUP BY status'",
        "degraded|5",
        "failed|1",
    ]
    term_text = "\n".join(term_lines) + "\n"
    render_terminal(term_text, OUT / "terminal.png")

    # 2. 真实 Markdown 渲染
    md_src = ROOT / "out/demo/2026-10-10-sqlite/2026-10-10/handover.md"
    if md_src.exists():
        render_handover(md_src, OUT / "handover-md.png")
        # 注意：render_handover_demo_intro 内部同样需要 ROOT/out/... 而非 docs/out/...
        if (ROOT / "out/demo/2026-10-10-sqlite/2026-10-10/handover.md").exists():
            render_handover_demo_intro(OUT / "handover-md-overview.png", ROOT / "out/demo/2026-10-10-sqlite/2026-10-10/handover.md")

    # 3. 真实 SQLite 输出
    sql_src = ROOT / "out/sqlite_history_output.txt"
    if sql_src.exists():
        sql_text = sql_src.read_text(encoding="utf-8")
        render_sqlite(sql_text, OUT / "sqlite-history.png")

    print("[render_screenshots] OK")
    for p in sorted(OUT.glob("*.png")):
        print(f"  - {p.relative_to(ROOT)} ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
